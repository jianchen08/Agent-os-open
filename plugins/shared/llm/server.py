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
import contextlib
import functools
import logging
import time as _time
import uuid
from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

from _config_models import (  # noqa: E402
    ModelConfigLoaderShim,
    get_config,
    normalize_model_id,
    resolve_model_id,
    set_config,
)
from _message_normalizer import normalize_messages_for_provider  # noqa: E402
from _root_api import (  # noqa: E402
    decode_body as _decode_body,
    json_response as _json_response,
    ok as _ok,
)
from adapter import StreamCancelledError  # noqa: E402  取消控制流异常单源（adapter 中断面）
from provider_adapters import resolve_replay_reasoning  # noqa: E402
from streaming import StreamTranslator, map_finish_reason  # noqa: E402

from agentos_plugin_sdk import AgentOSPlugin  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("llm_service")

# 全局 Adapter 实例
_adapter: Any = None

# 流式心跳间隔：chunk 静默超过该阈值即发 keepalive 事件（超时探活）。
# 消费端以 keepalive 区分"上游活着但慢"与"连接死了"。
KEEPALIVE_INTERVAL_SECONDS: float = 30.0

# 取消感知降级轮询间隔：主路径 = wait_run_stop 长轮询（事件驱动，感知延迟
# ≈信号传播）；长轮询通道故障（旧内核/调用异常）时回退为本间隔的
# get_run_status 轮询（llm_core 侧停止信号的降级感知节奏）。
CANCEL_POLL_INTERVAL_SECONDS: float = 0.5


@plugin.on_load
async def _on_load(params: dict[str, Any]) -> None:
    """Initialize LLM adapter on load."""
    global _adapter
    config = plugin.get_config()
    logger.info("LLM service loaded, config keys: %s", list(config.keys()) if config else "(empty)")

    # 注入配置到 _config_models shim（供 router_factory/adapter 的懒加载路径复用）
    set_config(config)

    # 重置 router_factory 模块级单例：on_load 在配置重注入时会重复触发，
    # 不重置则旧单例（旧 key/旧模型表）一直存活，新配置永不生效。
    from router_factory import reset_router  # noqa: PLC0415

    reset_router()

    # 共享 httpx 客户端池（F12 池化语义平移）：进程级单例连接复用——无池化时
    # 逐调用建连，半死连接残留即报 Unclosed（装机日志单日 140 次）。幂等，
    # 重复 on_load 复用活池。
    import wire_client as _wire  # noqa: PLC0415

    _wire._get_client()
    _adapter = None


@plugin.on_config_changed
def _on_config_changed(config: dict[str, Any]) -> None:
    """每调用配置变更感知：刷新注入视图并重建 adapter/router。

    内核每次调用现算下发用户空间叠加后的最新配置——不刷新则模型表/key 池
    冻结在 sidecar 启动快照，设置页新增模型/换 key 须重启才生效
    （2026-09-28 事故根因之二，llm_service 侧半边）。重建沿用 on_load 同款
    语义：reset_router 清模块级单例，_adapter 置空由 _ensure_adapter 懒重建
    （在途流持有旧对象引用不受影响，调用边界触发不引入中途换装）。
    """
    global _adapter
    set_config(config)

    from router_factory import reset_router  # noqa: PLC0415

    reset_router()
    _adapter = None
    logger.info("LLM 配置变更感知：router/adapter 已重置，下次调用懒重建")


@plugin.on_unload
async def _on_unload(params: dict[str, Any]) -> None:
    """Cleanup on unload."""
    global _adapter
    _adapter = None
    # 共享池全路径关闭（连接与缓冲随 unload 释放，不留半开会话）
    import wire_client as _wire  # noqa: PLC0415

    await _wire.aclose_shared_client()


def _ensure_adapter() -> Any:
    """延迟初始化 adapter（首次调用时构建）。"""
    global _adapter
    if _adapter is not None:
        return _adapter

    from router_factory import build_adapter  # noqa: PLC0415

    # provider 插件契约调用方注入（2026-11-04 网关化第二步）：via=plugin 模型
    # 经内核 CapabilityHandlerRegistry 路由到 provides 方（provides 注册表解析
    # 提供者，缺失由 adapter 分支 fail-closed）。
    async def _plugin_provider_caller(ns, method, params, timeout=None):  # noqa: ANN001,ANN202
        return await plugin.get_capability(ns).call(method, params, timeout)

    _pending_caller = _plugin_provider_caller

    # 构建前幂等重注入并读已解析副本：内核下发的配置含 ``${VAR}`` 占位符，
    # 解析（进程环境 → 项目根 .env 兜底）只发生在 ``set_config``。直接用
    # ``plugin.get_config()`` 原文构建会把字面量 ``${DEEPSEEK_API_KEY}`` 灌进
    # KeySlot/Router 单例，上游恒 401。on_load 之后配置也可能更新（重注入），
    # 重注入保证懒构建拿到的一定是最新已解析值。
    set_config(plugin.get_config())
    model_loader = _ModelLoaderShim(get_config())
    _adapter = build_adapter(model_loader)
    if hasattr(_adapter, "set_plugin_provider_caller"):
        _adapter.set_plugin_provider_caller(_pending_caller)
    logger.info("LLM adapter initialized: %s", type(_adapter).__name__)
    return _adapter


class _ModelLoaderShim(ModelConfigLoaderShim):
    """server.py 侧的 model_loader 句柄（供 ``_ensure_adapter`` 构建时传参）。

    复用 ``_config_models.ModelConfigLoaderShim`` 的 ``load_llm_data`` 实现，
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

    流式消费循环同步密集调 on_chunk，若直接 await notify 推送，task 会堆积
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
        self._keepalive_interval = keepalive_interval if keepalive_interval is not None else KEEPALIVE_INTERVAL_SECONDS
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
        """同步入队（O(1) 不阻塞，流式消费循环安全调用）。"""
        envelope = dict(self._envelope)
        envelope["sequence"] = self._seq
        self._seq += 1
        self._last_event_monotonic = _time.monotonic()
        self._queue.put_nowait((event, {**envelope, **payload}))

    def emit_plugin_error(self, error: dict[str, Any], plugin_id: str) -> None:
        """以现役 plugin_error 契约事件推送非终止警示（观测C，零新事件类型）。

        - message_id 合成 p_ 命名空间：本插件非 conduit，网关按 plugin owner
          执法；调用信封携带的 a_ id（内核签发给引擎路径）不可复用。
        - thread_id/pipeline_id 缺席（无 _call_context 的直连调用）时跳过：
          网关按缺路由键必丢，与其发不可达事件不如显式不发。
        """
        if not self._envelope.get("thread_id") or not self._envelope.get("pipeline_id"):
            logger.debug("[llm] plugin_error 缺 thread_id/pipeline_id 路由键，跳过推送")
            return
        self.put(
            "plugin_error",
            {
                "pipeline_id": self._envelope["pipeline_id"],
                "message_id": f"p_fallback_{uuid.uuid4().hex}",
                "thread_id": self._envelope["thread_id"],
                "plugin_id": plugin_id,
                "error": error,
            },
        )

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


# 长轮询单窗时长（秒）：wait_run_stop 挂起等待的上限，到点无信号 = run 正常
# 在跑，重挂下一窗。事件驱动取消（用户裁定 2026-10-21）后，旧 0.5s 轮询
# 间隔只作为通道故障的降级节奏保留。
CANCEL_WATCH_WINDOW_SECONDS: float = 20.0
# 长轮询 capability 调用超时 = 窗口 + 内核侧余量（SDK 默认 30s 也能覆盖，
# 显式传值防默认值上调后误伤）。
CANCEL_WATCH_CALL_TIMEOUT_SECONDS: float = 25.0


async def _watch_run_cancel(
    handle: Any,
    run_id: str,
    cancel_event: asyncio.Event,
) -> None:
    """事件驱动等待 run 停止信号（长轮询）。

    内核 `pipeline-executor.wait_run_stop` 挂起等待，dispatch_stop/suspend
    落标（run_status=suspended）即经内核停止信号中枢即时唤醒——取消感知
    延迟从轮询间隔降为信号传播本身。窗口到点无信号 = run 正常在跑，循环
    重挂。长轮询通道故障（旧内核无该方法/调用异常）降级回原 get_run_status
    轮询节奏——取消感知是增强能力，通道故障不阻断流式主流程（best-effort
    契约不变）。
    """
    while not cancel_event.is_set():
        try:
            resp = await handle.call(
                "wait_run_stop",
                {"run_id": run_id, "timeout_secs": CANCEL_WATCH_WINDOW_SECONDS},
                CANCEL_WATCH_CALL_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001 —— 通道故障降级回轮询（best-effort）
            await asyncio.sleep(CANCEL_POLL_INTERVAL_SECONDS)
            try:
                resp = await handle.call("get_run_status", {"run_id": run_id})
            except Exception:  # noqa: BLE001 —— 轮询失败继续下一窗（best-effort）
                continue
        if isinstance(resp, dict) and resp.get("status") == "suspended":
            logger.info("[llm] run 停止信号已确认，中断流式输出 run_id=%s", run_id)
            cancel_event.set()
            return


class _StreamOutbound:
    """流事件出站管道：chunk 归一化累积 → 事件翻译 → event-bus 推送 → 幂等收尾。

    event-bus 未注入时推送降级（publisher=None）：chunk 仍经翻译器消费，
    仅不推送（返回值与信封语义不变，调用方不感知通道差异）。
    """

    def __init__(self, publisher: Any | None, cancel_event: asyncio.Event) -> None:
        self._publisher = publisher
        self._translator = StreamTranslator()
        self._accumulator = _PartialAccumulator()
        self._cancel_event = cancel_event

    @property
    def accumulator(self) -> _PartialAccumulator:
        return self._accumulator

    def has_content(self) -> bool:
        return self._accumulator.has_content()

    def snapshot(self) -> dict[str, Any]:
        return self._accumulator.build_snapshot()

    def on_chunk(self, chunk_data: dict[str, Any]) -> str | None:
        """adapter 归一化 chunk → 累积部分内容 → 翻译 → 入队推送。

        同步闭包（流循环安全调用）。轮询器已感知 run suspended（停止信号）
        时抛 ``StreamCancelledError`` 中断流消费——异常从 on_chunk 处传播，
        adapter 的 consume 循环与 finally（aclose/许可释放）照常收尾。

        ``fallback_notice``（观测C：key 失败→Router fallback 的降级标记）在
        累积/翻译之前拦截——警告不是 assistant 内容，不得混入正文与半截
        快照；翻译为现役 plugin_error 契约事件（非终止，前端只弹通知中心）。
        """
        if chunk_data.get("type") == "fallback_notice":
            self._publish_fallback_notice(chunk_data)
            return None
        self._accumulator.accumulate(chunk_data)
        for event in self._translator.translate(chunk_data):
            if self._publisher is not None:
                self._publisher.put(event.event, event.payload)
        if self._cancel_event.is_set():
            raise StreamCancelledError("run suspended, stream cancelled")
        return None

    def _publish_fallback_notice(self, notice: dict[str, Any]) -> None:
        """key 失败→Router fallback 的用户可见信号（观测C，禁静默降级）。

        影响矩阵「LLM 通道挂→报错可见禁静默降级」：fallback 轮此前对用户
        完全不可见（只见变慢）；现役 plugin_error 形态 = 非终止信号，消息
        正常收尾，前端只弹通知中心。文案零 key 零口令，provider/model 进
        details 供诊断。
        """
        if self._publisher is None:
            return
        error: dict[str, Any] = {
            "code": "LLM_KEY_FALLBACK",
            "message": str(notice.get("message") or "部分 API key 暂不可用，已自动切换备用"),
            "source": "llm",
            "retryable": True,
        }
        details = {k: notice[k] for k in ("provider", "model") if notice.get(k)}
        if details:
            error["details"] = details
        self._publisher.emit_plugin_error(error, "llm_service")

    def finalize(self, reason: str, usage: dict[str, Any] | None = None) -> None:
        """收尾（含断流兜底）：闭块 → usage → finish；finish 幂等保证异常路径
        补发的 finish{reason:error} 不会与正常路径重复。"""
        for event in self._translator.finish(reason, usage=usage):
            if self._publisher is not None:
                self._publisher.put(event.event, event.payload)


async def _run_cancelled_at_start(poll_handle: Any, run_id: str) -> bool:
    """execute 起手检查：run 已 suspended（停止信号在调用前已落地）→ True。

    检查失败按未取消继续（best-effort）。
    """
    try:
        resp = await poll_handle.call("get_run_status", {"run_id": run_id})
    except Exception:  # noqa: BLE001 —— 起手检查失败按未取消继续（best-effort）
        return False
    return isinstance(resp, dict) and resp.get("status") == "suspended"


def _backfill_model_defaults(
    model: str,
    temperature: float | None,
    max_tokens: int | None,
    kwargs: dict[str, Any],
) -> tuple[float | None, int | None, dict[str, Any] | None]:
    """参数回填（单一真值 = llm.yaml models.<id>.default_params）。

    调用方（llm_core/压缩等）只需给模型名；未显式传的参数按模型条目
    default_params 逐键补齐，显式传参恒优先（setdefault 语义）。模型
    条目未配置的键不发——不发明兜底值（如 0.7/4096），上游按模型自身
    默认运行（2026-09-03 用户裁定）。

    Returns:
        (temperature, max_tokens, model_entry)——回填后的采样参数与模型条目
        （provider 解析与回填共用一次配置读取）。
    """
    model_entry = ModelConfigLoaderShim(get_config()).get_model_config(model)
    default_params = model_entry.get("default_params") if isinstance(model_entry, dict) else None
    if isinstance(default_params, dict):
        if temperature is None:
            temperature = default_params.get("temperature")
        if max_tokens is None:
            max_tokens = default_params.get("max_tokens")
        for dp_key, dp_value in default_params.items():
            if dp_key not in ("temperature", "max_tokens"):
                kwargs.setdefault(dp_key, dp_value)
    return temperature, max_tokens, model_entry


def _normalize_inbound_messages(
    messages: list[dict[str, Any]],
    model_entry: dict[str, Any] | None,
    pipeline_id: str,
    model: str,
) -> list[dict[str, Any]]:
    """msg 标准化唯一关卡（adapter 调用前，所有消费方无差别）。

    消息标准化属大模型调用面职责，单一真值源在本服务：结构标准化 +
    配对校验/孤儿摘除（无状态全量，幂等）+ MiniMax 三阶段。provider 按
    llm.yaml 模型条目解析；模型不在配置表时按通用段处理（配对校验
    provider 无关），MiniMax 专有段由 adapter 侧角色兜底最后防线补位。
    """
    provider = str(model_entry.get("provider", "") or "") if isinstance(model_entry, dict) else ""
    inbound_count = len(messages)
    normalized = normalize_messages_for_provider(
        messages,
        provider=provider,
        name="llm_service",
        pipeline_id=pipeline_id,
    )
    if len(normalized) != inbound_count:
        logger.debug(
            "[llm] msg normalize: %d → %d 条（孤儿 tool result/未配对 assistant 摘除）provider=%s model=%s",
            inbound_count,
            len(normalized),
            provider,
            model,
        )
    return normalized


def _build_call_kwargs(
    kwargs: dict[str, Any],
    temperature: float | None,
    max_tokens: int | None,
) -> dict[str, Any]:
    """组装 adapter 调用参数。

    透传 llm_core 经 kwargs 携带的模型参数（llm.yaml default_params
    的 thinking/reasoning_effort 等）：模型参数必须经 kwargs 全量
    上送，配置的思考预算才生效。_call_context 是内核路由信封
    （thread/pipeline/message 键），非模型参数，pop 不外传。
    回填后仍未设置的采样参数不携带（None 不发给上游）——
    请求里只出现模型条目或调用方显式写过的参数。
    """
    kwargs.pop("_call_context", None)
    call_kwargs: dict[str, Any] = dict(kwargs)
    if temperature is not None:
        call_kwargs["temperature"] = temperature
    if max_tokens is not None:
        call_kwargs["max_tokens"] = max_tokens
    return call_kwargs


@plugin.tool(
    name="model.resolve",
    schema={
        "type": "object",
        "properties": {
            "model_id": {
                "type": "string",
                "description": "Explicit model id (highest precedence; empty when absent)",
            },
            "model_tier": {
                "type": "string",
                "description": "Tier word (large/medium/small), resolved via defaults.tiers when no explicit id",
            },
        },
    },
    description=(
        "Resolve the effective model id with llm_core selection precedence: "
        "explicit model_id > defaults.tiers[model_tier] > defaults.chat; "
        "returns {model_id} (empty string when llm.yaml has no defaults)."
    ),
)
async def model_resolve(model_id: str = "", model_tier: str = "") -> dict[str, Any]:
    """解析最终模型 id（落定键单源：与 llm_core 选模共用 ``resolve_model_id``）。"""
    return {"model_id": resolve_model_id(model_id, model_tier)}


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
            "temperature": {
                "type": "number",
                "description": "Sampling temperature; omitted = filled per llm.yaml model entry default_params, sent only if neither is configured",
            },
            "max_tokens": {
                "type": "integer",
                "description": "Maximum output tokens; omitted = filled per model config, sent only if neither is configured",
            },
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
    temperature: float | None = None,
    max_tokens: int | None = None,
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
        temperature: 采样温度；None = 按 llm.yaml 模型条目 default_params
            回填，模型条目也未配置则不携带该参数。
        max_tokens: 最大输出 token；None = 按模型配置回填，均未配置则不携带。
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

    # 模型 id 归一（模型路由唯一标识，单点执法）：调用方可能传上游模型名
    # （model_name，如压缩链路「跟随管道当前模型」），而查表（models 段条目
    # / provider / 凭证 / 超时）、KeyPool 选路、Router 部署全部以 model id
    # 为基础——不归一则查不到条目与 provider，落 Router 报「no healthy
    # deployments」。归一后 model 恒为 model id。
    model = normalize_model_id(model)

    # agent 层级优先级透传：llm_core 调用方进程的 contextvar 不跨进程共享，
    # KeyPool 信号量的优先级排队读的是本进程 contextvar——显式接收并落位。
    # 弹出后不再进 kwargs（避免透传上游触发参数拒绝）。
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

    # 取消感知（域门控：仅当调用方显式带 run_id 才启用——llm_core 从
    # state.run_id 注入；任务管道/无 run_id 的调用不启动轮询，避免误伤
    # 任务域暂停语义）。轮询器见 suspended 置 cancel_event；取消经两条
    # 通道即时中断流消费：流活跃时 on_chunk 在下一个 chunk 处抛
    # StreamCancelledError（检查点），流静默（上游不吐 chunk/首 token
    # 预取）时 adapter 的可中断等待与该信号竞速，停止延迟=信号传播本身。
    run_id = kwargs.pop("run_id", None) or ""
    cancel_event = asyncio.Event()
    poll_task: asyncio.Task[None] | None = None

    publisher = _StreamPublisher(bus, envelope) if bus is not None else None
    outbound = _StreamOutbound(publisher, cancel_event)

    if publisher is not None:
        publisher.start()

    try:
        if run_id:
            try:
                poll_handle = plugin.get_capability("pipeline-executor")
            except KeyError:
                poll_handle = None
            if poll_handle is not None:
                if await _run_cancelled_at_start(poll_handle, run_id):
                    logger.info("[llm] run 起手检查已 suspended，跳过 LLM 调用 run_id=%s", run_id)
                    outbound.finalize("error")
                    return _partial_result(stream_id, "interrupted", outbound.snapshot())
                poll_task = asyncio.create_task(_watch_run_cancel(poll_handle, run_id, cancel_event))

        temperature, max_tokens, model_entry = _backfill_model_defaults(model, temperature, max_tokens, kwargs)
        messages = _normalize_inbound_messages(messages, model_entry, envelope["pipeline_id"], model)
        # rc 回放档位（2026-10-21 默认档=回放）：按模型条目解析（条目声明 >
        # provider 剥离名单 > 默认回放）。专用参数传适配器——**不得进 call_kwargs**
        # （会随 kwargs 进上游请求体触发参数拒绝）。
        replay_reasoning = resolve_replay_reasoning(model_entry)

        try:
            call_kwargs = _build_call_kwargs(kwargs, temperature, max_tokens)
            response = await adapter.completion(
                model=model,
                messages=messages,
                tools=tools,
                stream=True,
                on_chunk=outbound.on_chunk,
                replay_reasoning=replay_reasoning,
                cancel_event=cancel_event,
                **call_kwargs,
            )
        except StreamCancelledError:
            # 调用方停止（run suspended → cancel_event 置位：流活跃走 on_chunk
            # 检查点、流静默走 adapter 可中断等待）：半截内容经返回 dict 的
            # partial 字段交付（取消是预期终止，不传播异常）。
            outbound.finalize("error")
            return _partial_result(stream_id, "interrupted", outbound.snapshot())
        except BaseException as exc:
            # 任务级取消（内核/上层取消本请求）必须原样传播，不得转成业务返回。
            if isinstance(exc, asyncio.CancelledError):
                raise
            # 断流兜底：finish 前异常（网络/超时/上游错误）→ 补发 finish{reason:error}，
            # 消费端据此终止等待（参考 DSH [DONE] 缺失 = STREAM_CLOSED 语义）。
            outbound.finalize("error")
            # 流已开始且累积了内容 → 半截内容经返回 dict 的 partial 字段交付
            # （partial 是返回值的一部分，可跨进程传输；异常属性无法过进程边界）。
            # 流未开始/零累积 → 分类后上抛（wire/httpx 异常换发 LLMProviderError，
            # 稳定中文前缀随 MCP 错误链透传到内核折叠文案；未分类异常原样
            # 上抛不误归因，取消/任务级取消已在上方分支处理）。
            if outbound.has_content():
                return _partial_result(stream_id, "error", outbound.snapshot(), exc=exc)
            from exceptions import classified_provider_raise  # noqa: PLC0415

            classified_provider_raise(exc, model)
        outbound.finalize(
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


@plugin.tool(
    name="llm.repair_json",
    schema={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Possibly broken/truncated JSON string"},
        },
        "required": ["text"],
    },
    description=(
        "Attempt to repair a common JSON defect (markdown fence, trailing commas, "
        "single quotes, truncation). Returns the repaired string, or null when "
        "unrepairable."
    ),
)
async def llm_repair_json(text: str) -> dict[str, Any]:
    """JSON 修复能力（llm_service 能力面收敛点）。

    ``repair_json_string`` 的单一真值源在本插件的 ``_message_normalizer``；
    本服务把该纯函数暴露到能力面，供确需共享的工具函数消费方
    （param_inject 参数兜底修复 / tool_schema_validator 截断检测——工具
    arguments 面，不被消息标准化覆盖）经 capability 调用复用。

    Args:
        text: 可能残缺/带噪声的 JSON 字符串。

    Returns:
        ``{"repaired": str | None}``——无法修复时 repaired 为 None。
    """
    from _message_normalizer import repair_json_string  # noqa: PLC0415

    return {"repaired": repair_json_string(text)}


# ══ http.handle 响应封装：公共实现 plugins/shared/http_json.py（文件头已导入）══


# ══ 域分发（thinking-mode + config/llm）══

_THINKING_MODE_PREFIX = "/ext/llm_service/thinking-mode"
_CONFIG_LLM_PREFIX = "/ext/llm_service/config/llm"


def _api_error_response(exc: Exception) -> dict[str, Any]:
    """把域业务异常（含 status_code + message/detail）转 HTTP 响应（404/400/409/502）。"""
    status = int(getattr(exc, "status_code", 500) or 500)
    message = getattr(exc, "message", None) or getattr(exc, "detail", None) or str(exc)
    return _ok(_json_response({"detail": message}, status))


async def _handle_thinking_mode(path: str, method: str, raw_body: str) -> dict[str, Any]:
    """thinking-mode 域 6 端点：字面量 (sub,method) 表 + 两个 GET 前缀参数路由。"""
    import routes_thinking_mode as rtm  # noqa: PLC0415

    sub = path[len(_THINKING_MODE_PREFIX) :]
    literal = {
        ("GET", "/healthz"): rtm.health,
        ("GET", "/models"): rtm.list_models,
        ("POST", "/switch"): functools.partial(rtm.switch_mode, _decode_body(raw_body)),
        ("POST", "/recommendations"): functools.partial(rtm.recommendations, _decode_body(raw_body) or None),
    }
    handler: Any = literal.get((method, sub))
    if handler is not None:
        return _ok(_json_response(handler()))
    prefixed_get = (
        ("/models/", rtm.get_model_info),
        ("/check/", rtm.check_support),
    )
    for prefix, fn in prefixed_get:
        if method == "GET" and sub.startswith(prefix):
            return _ok(_json_response(fn(sub[len(prefix) :])))
    logger.warning("llm http.handle: no thinking-mode route for sub=%s method=%s", sub, method)
    return _ok(_json_response({"error": "not found", "path": path}, 404))


async def _handle_config_llm(
    path: str, method: str, raw_body: str, query: dict[str, str] | None = None
) -> dict[str, Any]:
    """config/llm 段端点：字面量 (sub,method) 表 + providers/thinking-levels 参数族。"""
    import routes_llm_config as rlc  # noqa: PLC0415

    sub = path[len(_CONFIG_LLM_PREFIX) :]  # "" / "/provider-types" / "/presets" / ...
    literal = {
        ("GET", "/provider-types"): rlc.get_provider_types,
        ("GET", "/presets"): rlc.get_llm_presets,
    }
    handler: Any = literal.get((method, sub))
    if handler is not None:
        return _ok(_json_response(handler()))

    # 思考参数组选项（聊天页选择器选项真值源）：model 走 query（模型名可能含
    # `/`，不进路径段）
    if method == "GET" and sub == "/thinking-levels":
        return _ok(_json_response(rlc.get_thinking_levels((query or {}).get("model", ""))))

    # 参数族：/providers/{id}/remote-models（先判，防被 {id} 前缀吞）
    # 2026-09-28 批次 A1：config/llm 其余文件 IO 端点（yaml CRUD + .env 直写）
    # 已退役——llm.yaml 读写收口到内核单一配置面
    # /api/v1/plugins/llm_service/config/llm，provider key 走 /api/v1/config/env。
    if method == "GET" and sub.startswith("/providers/") and sub.endswith("/remote-models"):
        provider_id = sub[len("/providers/") : -len("/remote-models")]
        return _ok(_json_response(rlc.get_remote_models(provider_id)))
    logger.warning("llm http.handle: no config/llm route for sub=%s method=%s", sub, method)
    return _ok(_json_response({"error": "not found", "path": path}, 404))


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
    """按 path 分发：thinking-mode 域 6 端点 + config/llm 段 4 只读端点
    （presets / provider-types / thinking-levels / remote-models；文件 IO
    端点已于批次 A1 退役，收口到内核单一配置面）。

    路径语义与原 /ext/channel_api/thinking-mode/** 与 /ext/channel_api/
    config/llm/** 逐项对齐（前端消费同一响应形态）；auth 由 http_endpoints
    auth=user 声明（dispatcher 层），handler 不读 _user。业务异常
    （status_code 属性）转对应 HTTP 状态，错误 body 形态与 FastAPI 版一致
    （``{"detail": ...}``）。域内匹配委托 _handle_thinking_mode/_handle_config_llm。
    """
    del plugin_id
    try:
        if path.startswith(_THINKING_MODE_PREFIX):
            return await _handle_thinking_mode(path, method, raw_body)
        if path.startswith(_CONFIG_LLM_PREFIX):
            return await _handle_config_llm(path, method, raw_body, query)
        return _ok(_json_response({"error": "not found", "path": path}, 404))
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "status_code"):
            return _api_error_response(exc)
        logger.error("llm http.handle 未预期错误: %s", exc, exc_info=True)
        return _ok(_json_response({"error": "internal server error", "detail": str(exc)}, 500))


if __name__ == "__main__":
    plugin.run()
