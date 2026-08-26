#!/usr/bin/env python3
"""LLM Service MCP 服务端——纯接口适配层。

老代码从 0.1 src/llm/ 原封不动复制到本目录（平铺），
本文件只做接口适配：调用老代码逻辑，通过 MCP SDK 暴露为工具。

核心能力：
- llm.complete_stream: 统一 LLM 调用（流式，DSH 8 事件协议经 event-bus 推送）
- llm.health_check: 检查模型是否可用

同时承载 thinking-mode 域与 config/llm 段 HTTP 面：
``http.handle`` 按 path 分发（协议与 agent_manager/monitoring 同款），
plugin.json ``http_endpoints`` 声明（/ext/llm_service/thinking-mode/** 与
/ext/llm_service/config/llm/**）；业务函数在 ``routes_thinking_mode.py``
与 ``routes_llm_config.py``。

[来源: docs/working/module_migration_plan.md §六 P2 迁移]
[来源: docs/working/LLM流式服务契约与Native迁移评估_20260826.md §二]
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import sys
import time as _time
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(__file__))

from _config_models import ModelConfigLoaderShim, set_config  # noqa: E402
from streaming import StreamTranslator, map_finish_reason  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("llm_service")

# 全局 Adapter 实例
_adapter: Any = None

# 流式心跳间隔：chunk 静默超过该阈值即发 keepalive 事件（超时探活）。
# 消费端以 keepalive 区分"上游活着但慢"与"连接死了"。
KEEPALIVE_INTERVAL_SECONDS: float = 30.0

# 取消轮询间隔：llm_core 侧会话停止（dispatch_stop 置 run suspended）后，
# 本服务最迟在该间隔内感知并中断流（方案 §四.1 的 ~500ms 轮询）。
CANCEL_POLL_INTERVAL_SECONDS: float = 0.5


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize LLM adapter on load."""
    global _adapter
    config = plugin.get_config()
    logger.info("LLM service loaded, config keys: %s", list(config.keys()) if config else "(empty)")

    # 注入配置到 _config_models shim（供 router_factory/adapter 的懒加载路径复用）
    set_config(config)

    # 延迟构建 adapter：需要 model_loader（由配置注入）
    # 如果配置链路未修复，adapter 保持 None，工具调用时再延迟初始化
    _adapter = None


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    global _adapter
    _adapter = None


def _ensure_adapter() -> Any:
    """延迟初始化 adapter（首次调用时构建）。"""
    global _adapter
    if _adapter is not None:
        return _adapter

    from router_factory import build_adapter  # noqa: PLC0415

    # 构建 model_loader shim：从 plugin 配置中读取 LLM 配置
    config = plugin.get_config()
    model_loader = _ModelLoaderShim(config)
    _adapter = build_adapter(model_loader)
    logger.info("LLM adapter initialized: %s", type(_adapter).__name__)
    return _adapter


class _ModelLoaderShim(ModelConfigLoaderShim):
    """server.py 侧的 model_loader 句柄（供 ``_ensure_adapter`` 构建时传参）。

    复用 ``_config_models.ModelConfigLoaderShim`` 的 ``_load_llm_data`` 实现，
    确保三条取配置路径（本类 / ``router_factory`` / ``adapter._route_call``）
    行为一致：P1 起统一从 ``config["llm"]`` 取值（config_files 命名空间）。
    """


def _resolve_envelope(arguments: dict[str, Any]) -> dict[str, str]:
    """从工具参数解析事件信封路由键（thread_id/pipeline_id/message_id）。

    调用方（tool_core 引擎路径）经 tool-executor._call_context 把前端路由键
    （thread_id/pipeline_id/message_id）合入工具参数（内核 capability_router
    透传契约）；直接调用（无上下文）时为空串——event-bus 推送仍携带键位，
    由内核按 thread_id 空值丢弃/透传语义处理。
    """
    ctx = arguments.get("_call_context") or {}
    return {
        "thread_id": str(ctx.get("thread_id", "") or ""),
        "pipeline_id": str(ctx.get("pipeline_id", "") or ""),
        "message_id": str(ctx.get("message_id", "") or ""),
    }


class _StreamPublisher:
    """流式事件推送器：Queue + 独立消费者（对照 llm_core/server.py 既有模式）。

    litellm 流式循环同步密集调 on_chunk，若直接 await notify 推送，task 会堆积
    到 LLM 循环结束才一起执行（事件循环没机会切换），导致所有 chunk 最后一次性
    到达。Queue.put_nowait 是 O(1) 不阻塞，消费者协程 await get() 异步取出推送，
    与流式循环并发，实现真正逐字实时推送。

    fire-and-forget：notify 失败（通道关闭等）静默降级——流式出口不阻断
    LLM 主流程。信封（thread_id/pipeline_id/message_id）由构造时确定，
    sequence 进程内单调递增（仅调试定位，非消息权威 seq）。

    心跳：独立心跳任务周期检查静默时长（距上个事件），超过
    KEEPALIVE_INTERVAL_SECONDS 即发 keepalive 事件（超时探活）——消费端据此
    区分"上游活着但慢"与"连接死了"。
    """

    def __init__(
        self,
        bus: Any,
        envelope: dict[str, str],
        keepalive_interval: float | None = None,
    ) -> None:
        self._bus = bus
        # 信封在推送时补 sequence（int），故内部用宽松 dict
        self._envelope: dict[str, Any] = dict(envelope)
        # 模块级常量调用时读取（测试可 monkeypatch 模块属性生效）
        self._keepalive_interval = (
            keepalive_interval if keepalive_interval is not None else KEEPALIVE_INTERVAL_SECONDS
        )
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
        self._seq = 0
        self._last_event_monotonic = _time.monotonic()
        self._consumer: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动消费者与心跳协程（在调用方事件循环上）。"""
        loop = asyncio.get_event_loop()
        self._consumer = loop.create_task(self._consume())
        self._heartbeat = loop.create_task(self._heartbeat_loop())

    async def _consume(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                break  # 哨兵：流结束终止消费者
            event, payload = item
            try:
                await self._bus.notify("emit", {"event": event, "payload": payload})
            except Exception:
                logger.debug("event-bus.emit 推送失败（fire-and-forget 降级）: %s", event)

    async def _heartbeat_loop(self) -> None:
        """周期检查静默时长：超过阈值发 keepalive（活跃流不打扰）。"""
        while True:
            await asyncio.sleep(self._keepalive_interval)
            if _time.monotonic() - self._last_event_monotonic >= self._keepalive_interval:
                self.put("keepalive", {})

    def put(self, event: str, payload: dict[str, Any]) -> None:
        """同步入队（O(1) 不阻塞，litellm 流式循环安全调用）。"""
        envelope = dict(self._envelope)
        envelope["sequence"] = self._seq
        self._seq += 1
        self._last_event_monotonic = _time.monotonic()
        self._queue.put_nowait((event, {**envelope, **payload}))
    async def stop(self) -> None:
        """发哨兵并等待消费者排空剩余事件（保证不丢末尾）。"""
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat
        self._queue.put_nowait(None)
        if self._consumer is not None:
            with contextlib.suppress(Exception):
                await self._consumer


class _PartialAccumulator:
    """流式部分内容累积器——异常/取消时产出半截快照（与批次 A snapshot 同构）。

    从 adapter 归一化 chunk（on_chunk 契约：``{"type","content"}`` 与
    ``{"type": "tool_call", "tool_calls": [...]}``）累积 text/thinking/
    tool_calls；usage 由调用方在正常返回时从 adapter 响应补入（流中断时
    adapter 内部 usage 不可达，快照 usage 为 None——批次 A 同语义：可选）。
    """

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._thinking_parts: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}

    def accumulate(self, chunk: dict[str, Any]) -> None:
        """累积单个归一化 chunk（同步闭包，流循环安全调用）。"""
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")
        if chunk_type == "thinking":
            if content:
                self._thinking_parts.append(content)
        elif chunk_type == "tool_call":
            for tc in chunk.get("tool_calls", []):
                idx = max(int(getattr(tc, "index", 0)), 0)
                entry = self._tool_calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                tc_id = getattr(tc, "id", None) or ""
                if tc_id and not entry["id"]:
                    entry["id"] = tc_id
                function = getattr(tc, "function", None)
                if function is not None:
                    if function.name:
                        entry["name"] += function.name
                    if function.arguments:
                        entry["arguments"] += function.arguments
        elif content:
            self._text_parts.append(content)

    def has_content(self) -> bool:
        """是否已累积任何内容（text/thinking/tool_calls 任一非空）。"""
        return bool(self._text_parts or self._thinking_parts or self._tool_calls)

    def build_snapshot(self) -> dict[str, Any]:
        """产出与批次 A snapshot 同构的部分内容快照（usage 由调用方补）。"""
        return {
            "text": "".join(self._text_parts) if self._text_parts else None,
            "thinking_text": "".join(self._thinking_parts) if self._thinking_parts else None,
            "tool_calls": [
                {
                    "id": entry["id"] or f"call_{idx}",
                    "name": entry["name"],
                    "arguments": entry["arguments"],
                }
                for idx, entry in sorted(self._tool_calls.items())
            ],
            "usage": None,
        }


class StreamCancelledError(Exception):
    """流式调用被调用方停止（run suspended）——内部控制流异常，不出服务边界。"""


def _partial_result(
    stream_id: str,
    status: str,
    snapshot: dict[str, Any],
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """组装中断/错误返回：partial 快照 + 语义字段（status/finish_reason）。

    error 路径携带 ``llm_error_info``（错误类型与消息），llm_core 据此组装
    ``llm_error_info`` 落库；interrupted 路径无错误信息（取消不是错误）。
    """
    result: dict[str, Any] = {
        "status": status,
        "stream_id": stream_id,
        "partial": snapshot,
        "text": None,
        "tool_calls": [],
        "thinking_text": None,
        "usage": {},
        "finish_reason": "interrupted" if status == "interrupted" else "error",
    }
    if exc is not None:
        result["llm_error_info"] = {
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
    return result


async def _poll_run_cancel(
    handle: Any,
    run_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """轮询 run 状态：suspended → 置取消事件（llm_core 侧停止信号的感知锚）。

    dispatch_stop 把 run 置 Suspended（传输信号），本轮询器约每
    CANCEL_POLL_INTERVAL_SECONDS 查一次；见 suspended 即置事件，on_chunk
    在下一个 chunk 处返回 cancel 信号中断流消费。轮询失败静默继续——
    取消感知是增强能力，轮询通道故障不阻断流式主流程（best-effort）。
    """
    while not cancel_event.is_set():
        await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
        try:
            resp = await handle.call("get_run_status", {"run_id": run_id})
        except Exception:  # noqa: BLE001 —— 轮询失败继续下一轮（best-effort）
            continue
        if isinstance(resp, dict) and resp.get("status") == "suspended":
            logger.info("[llm] run 已 suspended（停止信号），中断流式输出 run_id=%s", run_id)
            cancel_event.set()
            return


@plugin.tool(
    name="llm.complete_stream",
    schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "LiteLLM model identifier (e.g. 'zai/glm-4-plus')"},
            "messages": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Chat messages array",
            },
            "tools": {
                "type": "array",
                "items": {"type": "object"},
                "description": "Optional tool schemas for function calling",
            },
            "temperature": {"type": "number", "default": 0.7},
            "max_tokens": {"type": "integer", "default": 4096},
        },
        "required": ["model", "messages"],
    },
    description=(
        "Send a streaming completion request to the LLM. "
        "Stream events are emitted via event-bus (block_start/text_delta/"
        "reasoning_delta/tool_call_delta/block_end/usage/finish/keepalive)."
    ),
)
async def llm_complete_stream(
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute a streaming LLM completion request.

    Uses the KeyPoolAdapter internally for multi-key pooling, rate limiting,
    and automatic fallback. The stream is translated to the DSH 8-event
    protocol (block-indexed) and pushed via the event-bus channel; the
    returned dict carries the full aggregated response (same shape as the
    adapter's LLMResponse) for callers that need the complete output.

    部分内容契约（llm_core 半截落库的事实源）：
    - 正常完成：``partial`` 恒为 None；
    - 流中途异常且已累积内容：返回 ``{"status": "error", "partial": {...}}``
      （partial 携带 text/thinking_text/tool_calls/usage 快照 + ``llm_error_info``），
      异常不传播——半截内容经返回值跨进程交付；流未开始/零累积维持 raise；
    - 调用方停止（run suspended，经 run_id 轮询感知）：返回
      ``{"status": "interrupted", "partial": {...}, "finish_reason": "interrupted"}``。

    Args:
        model: LiteLLM model identifier string.
        messages: Chat message list.
        tools: Optional tool schemas for function calling.
        temperature: Sampling temperature.
        max_tokens: Maximum tokens to generate.

    Returns:
        Dict with ``status`` / ``stream_id`` plus the full response:
        ``text`` (str|None), ``tool_calls`` (list), ``thinking_text``
        (str|None), ``usage`` (dict) and ``finish_reason`` (stop/length/
        tool_calls/error/interrupted). Chunk deltas also flow via event-bus
        events. ``partial`` 携带半截快照（仅 error/interrupted 路径非 None）。
    """
    adapter = _ensure_adapter()
    stream_id = f"stream_{uuid.uuid4().hex}"
    envelope = _resolve_envelope(kwargs)

    # agent 层级优先级透传：llm_core 调用方进程的 contextvar 不跨进程共享，
    # KeyPool 信号量的优先级排队读的是本进程 contextvar——显式接收并落位。
    # 弹出后不再进 kwargs（避免透传给 litellm 触发 UnsupportedParamsError）。
    agent_level = kwargs.pop("agent_level", None)
    if agent_level:
        from key_pool import set_agent_priority  # noqa: PLC0415

        set_agent_priority(str(agent_level))

    # event-bus 未注入时流式推送降级：chunk 仍经翻译器消费，仅不推送
    # （返回值与信封语义不变，调用方不感知通道差异）。
    try:
        bus = plugin.get_capability("event-bus")
    except KeyError:
        bus = None

    publisher = _StreamPublisher(bus, envelope) if bus is not None else None
    translator = StreamTranslator()
    accumulator = _PartialAccumulator()

    # 取消感知（域门控：仅当调用方显式带 run_id 才启用——llm_core 从
    # state.run_id 注入；任务管道/无 run_id 的调用不启动轮询，避免误伤
    # 任务域暂停语义）。轮询器见 suspended 置 cancel_event，on_chunk 在
    # 下一个 chunk 处返回 "cancel" 信号中断流消费（与重复检测 stop 信号
    # 同通道，互不干扰）。
    run_id = kwargs.pop("run_id", None) or ""
    cancel_event = asyncio.Event()
    poll_task: asyncio.Task[None] | None = None

    def _on_chunk(chunk_data: dict[str, Any]) -> str | None:
        """adapter 归一化 chunk → 累积部分内容 → 翻译 → 入队推送。

        同步闭包（流循环安全调用）。轮询器已感知 run suspended（停止信号）
        时抛 ``StreamCancelledError`` 中断流消费——异常从 on_chunk 处传播，
        adapter 的 consume 循环与 finally（aclose/许可释放）照常收尾。
        """
        accumulator.accumulate(chunk_data)
        for event in translator.translate(chunk_data):
            if publisher is not None:
                publisher.put(event.event, event.payload)
        if cancel_event.is_set():
            raise StreamCancelledError("run suspended, stream cancelled")
        return None

    # 收尾（含断流兜底）：闭块 → usage → finish；finish 幂等保证异常路径
    # 补发的 finish{reason:error} 不会与正常路径重复。
    def _finalize(reason: str, usage: dict[str, Any] | None = None) -> None:
        for event in translator.finish(reason, usage=usage):
            if publisher is not None:
                publisher.put(event.event, event.payload)

    if publisher is not None:
        publisher.start()

    try:
        if run_id:
            try:
                poll_handle = plugin.get_capability("pipeline-executor")
            except KeyError:
                poll_handle = None
            if poll_handle is not None:
                # execute 起手检查：run 已 suspended（停止信号在调用前已落地）
                # → 直接返回 interrupted，不起空 LLM 调用。
                try:
                    resp = await poll_handle.call("get_run_status", {"run_id": run_id})
                except Exception:  # noqa: BLE001 —— 起手检查失败按未取消继续（best-effort）
                    resp = None
                if isinstance(resp, dict) and resp.get("status") == "suspended":
                    logger.info("[llm] run 起手检查已 suspended，跳过 LLM 调用 run_id=%s", run_id)
                    _finalize("error")
                    return _partial_result(stream_id, "interrupted", accumulator.build_snapshot())
                poll_task = asyncio.create_task(_poll_run_cancel(poll_handle, run_id, cancel_event))

        try:
            response = await adapter.completion(
                model=model,
                messages=messages,
                tools=tools,
                stream=True,
                on_chunk=_on_chunk,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except StreamCancelledError:
            # 调用方停止（run suspended → on_chunk 返回 cancel → adapter 中断流）：
            # 半截内容经返回 dict 的 partial 字段交付（取消是预期终止，不传播异常）。
            _finalize("error")
            return _partial_result(stream_id, "interrupted", accumulator.build_snapshot())
        except BaseException as exc:
            # 任务级取消（内核/上层取消本请求）必须原样传播，不得转成业务返回。
            if isinstance(exc, asyncio.CancelledError):
                raise
            # 断流兜底：finish 前异常（网络/超时/上游错误）→ 补发 finish{reason:error}，
            # 消费端据此终止等待（参考 DSH [DONE] 缺失 = STREAM_CLOSED 语义）。
            _finalize("error")
            # 流已开始且累积了内容 → 半截内容经返回 dict 的 partial 字段交付
            # （partial 是返回值的一部分，可跨进程传输；异常属性无法过进程边界）。
            # 流未开始/零累积 → 维持 raise（现状不变，由调用方错误链处理）。
            if accumulator.has_content():
                return _partial_result(stream_id, "error", accumulator.build_snapshot(), exc=exc)
            raise
        _finalize(
            map_finish_reason(getattr(response, "finish_reason", None)),
            usage=getattr(response, "usage", None),
        )
    finally:
        if publisher is not None:
            await publisher.stop()
        if poll_task is not None:
            poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await poll_task

    return {
        "status": "streamed",
        "stream_id": stream_id,
        "text": response.text,
        "tool_calls": response.tool_calls or [],
        "thinking_text": response.thinking_text,
        "usage": response.usage or {},
        "finish_reason": map_finish_reason(getattr(response, "finish_reason", None)),
        # 正常完成：partial 恒为 None（半截快照只在异常/取消路径返回）
        "partial": None,
    }


@plugin.tool(
    name="llm.health_check",
    schema={
        "type": "object",
        "properties": {
            "model": {"type": "string", "description": "Model identifier to check"},
        },
        "required": ["model"],
    },
    description="Check if a specific LLM model is healthy and available",
)
async def llm_health_check(model: str) -> dict[str, Any]:
    """Check model availability.

    Args:
        model: LiteLLM model identifier string.

    Returns:
        Dict with 'healthy' boolean and model name.
    """
    adapter = _ensure_adapter()
    try:
        healthy = await adapter.health_check(model)
        return {"healthy": healthy, "model": model}
    except Exception as exc:
        logger.warning("Health check failed for %s: %s", model, exc)
        return {"healthy": False, "model": model, "error": str(exc)}


# ══ http.handle 响应封装（内核 HttpHandleResponse 约定，与 workspace/monitoring 同款）══


def _json_response(payload: Any, status: int = 200) -> dict[str, Any]:
    """包成内核期望的 HttpHandleResponse（body base64）。"""
    body_str = json.dumps(payload, default=str, ensure_ascii=False)
    body_b64 = base64.b64encode(body_str.encode("utf-8")).decode("ascii")
    return {
        "status": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": body_b64,
        "body_encoding": "base64",
    }


def _ok(data: Any) -> dict[str, Any]:
    return {"success": True, "data": data}


def _error(message: str, status: int = 503) -> dict[str, Any]:
    return {"success": False, "error": message, "data": _json_response({"error": message}, status)}


def _decode_body(raw_body: str) -> dict[str, Any]:
    """解码 http.handle 的 raw_body（base64 或明文 JSON）为 dict。"""
    if not raw_body:
        return {}
    try:
        try:
            decoded = base64.b64decode(raw_body).decode("utf-8")
            if not decoded.lstrip().startswith(("{", "[")):
                decoded = raw_body
        except Exception:  # noqa: BLE001
            decoded = raw_body
        return json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON body: {exc}") from exc


# ══ 域分发（thinking-mode + config/llm）══

_THINKING_MODE_PREFIX = "/ext/llm_service/thinking-mode"
_CONFIG_LLM_PREFIX = "/ext/llm_service/config/llm"


def _api_error_response(exc: Exception) -> dict[str, Any]:
    """把域业务异常（含 status_code + message/detail）转 HTTP 响应（404/400/409/502）。"""
    status = int(getattr(exc, "status_code", 500) or 500)
    message = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
    return _ok(_json_response({"detail": message}, status))


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/llm_service/** (thinking-mode + config/llm domains)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：thinking-mode 域 6 端点 + config/llm 段 13 端点。

    路径语义与原 /ext/channel_api/thinking-mode/** 与 /ext/channel_api/
    config/llm/** 逐项对齐（前端消费同一响应形态）；auth 由 http_endpoints
    auth=user 声明（dispatcher 层），handler 不读 _user。业务异常
    （status_code 属性）转对应 HTTP 状态，错误 body 形态与 FastAPI 版一致
    （``{"detail": ...}``）。
    """
    try:
        # ── thinking-mode 域 ──
        if path.startswith(_THINKING_MODE_PREFIX):
            import routes_thinking_mode as rtm  # noqa: PLC0415

            sub = path[len(_THINKING_MODE_PREFIX):]
            if sub == "/healthz" and method == "GET":
                return _ok(_json_response(rtm.health()))
            if sub == "/models" and method == "GET":
                return _ok(_json_response(rtm.list_models()))
            if sub.startswith("/models/") and method == "GET":
                model_name = sub[len("/models/"):]
                return _ok(_json_response(rtm.get_model_info(model_name)))
            if sub.startswith("/check/") and method == "GET":
                model_name = sub[len("/check/"):]
                return _ok(_json_response(rtm.check_support(model_name)))
            if sub == "/switch" and method == "POST":
                return _ok(_json_response(rtm.switch_mode(_decode_body(raw_body))))
            if sub == "/recommendations" and method == "POST":
                recs_body = _decode_body(raw_body) or None
                return _ok(_json_response(rtm.recommendations(recs_body)))

            logger.warning("llm http.handle: no thinking-mode route for sub=%s method=%s", sub, method)
            return _ok(_json_response({"error": "not found", "path": path}, 404))

        # ── config/llm 段 ──
        if path.startswith(_CONFIG_LLM_PREFIX):
            import routes_llm_config as rlc  # noqa: PLC0415

            sub = path[len(_CONFIG_LLM_PREFIX):]  # "" / "/providers" / "/models/xxx" ...
            if sub == "" and method == "GET":
                return _ok(_json_response(rlc.get_llm_config()))
            if sub == "/providers" and method == "GET":
                return _ok(_json_response(rlc.get_providers()))
            if sub == "/providers" and method == "POST":
                return _ok(_json_response(rlc.add_provider(_decode_body(raw_body))))
            if sub == "/provider-types" and method == "GET":
                return _ok(_json_response(rlc.get_provider_types()))
            if sub.startswith("/providers/") and sub.endswith("/remote-models") and method == "GET":
                provider_id = sub[len("/providers/"):-len("/remote-models")]
                return _ok(_json_response(rlc.get_remote_models(provider_id)))
            if sub.startswith("/providers/") and method == "PUT":
                provider_id = sub[len("/providers/"):]
                return _ok(_json_response(rlc.update_provider(provider_id, _decode_body(raw_body))))
            if sub.startswith("/providers/") and method == "DELETE":
                provider_id = sub[len("/providers/"):]
                return _ok(_json_response(rlc.delete_provider(provider_id)))
            if sub == "/models" and method == "GET":
                return _ok(_json_response(rlc.get_models()))
            if sub == "/models" and method == "POST":
                return _ok(_json_response(rlc.add_model(_decode_body(raw_body))))
            if sub.startswith("/models/") and method == "PUT":
                model_id = sub[len("/models/"):]
                return _ok(_json_response(rlc.update_model(model_id, _decode_body(raw_body))))
            if sub.startswith("/models/") and method == "DELETE":
                model_id = sub[len("/models/"):]
                return _ok(_json_response(rlc.delete_model(model_id)))
            if sub == "/defaults" and method == "GET":
                return _ok(_json_response(rlc.get_defaults()))
            if sub == "/defaults" and method == "PUT":
                return _ok(_json_response(rlc.save_defaults(_decode_body(raw_body))))

            logger.warning("llm http.handle: no config/llm route for sub=%s method=%s", sub, method)
            return _ok(_json_response({"error": "not found", "path": path}, 404))

        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            return _api_error_response(exc)
        logger.error("llm http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()
