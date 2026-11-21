# @feature: FP-T07 llm api | @ci: python-coverage
"""llm 适配服务面（``llm_adapter.execute``）——双服务面角色二（主方案 §6.2）。

复制锚点
- 源文件 A：``plugins/shared/llm/adapter.py`` @
  c0fe6922fc8cf68cc0c46b6ff1e11c0db021056a（批 2 收口基线）
  · ``KeyPoolAdapter._direct_call_with_slot`` 的 wire 发送段（model_name 反查 /
    api_base 组装 / body 构建 / stream·post 分派 / timeout 解析）→ 本文件
    ``_DialectWireAdapter._do_completion``（编排面 KeyPool/冷却/兜底/预算不复制
    ——它们留在网关，见 §6.3）。
  · ``completion()`` 的调用面参数（on_chunk / replay_reasoning / cancel_event）
    → 经 ``llm_stream.WireStreamAdapter`` 原样承接。
- 源文件 B：``plugins/shared/llm/server.py`` @ 同基线
  · ``_PartialAccumulator``（流式半截快照累积器）与 ``_partial_result``
    （interrupted/error 返回形态）→ 本文件同名件逐行同源。
- 源文件 C：``plugins/shared/_llm_streaming.py`` @ 同基线
  · ``_FINISH_REASON_MAP``（finish_reason 五值收敛）→ 本文件同名表。只复制
    该映射而不 import 共享根翻译器：适配面本批不产 DSH 事件，避免把
    simurg 退化检测引擎拖进 llm_core 进程（扩展面保留给批 5）。
- 改造清单（仅下列差异）：
  1. 配置面读取单源 ``_config_models``（注入视图），不 import 网关的
     router_factory/KeyPool/例外族（编排面职责）；
  2. 返回 dict 与 ``llm.complete_stream`` 同构（status/stream_id/text/
     tool_calls/thinking_text/usage/finish_reason/stream_repetition/partial），
     增 ``dialect`` 诊断键；不含退化 suspect 字段（口令条 = 网关翻译器判定）；
  3. body 出站内部键黑名单在源清单上补 ``max_thinking_chars``（源件该键会
     随 kwargs 泄漏进 wire body——复制件修死，见 §7.2 代码方式铁律的
     「最小真改」边界）；
  4. 取消传播 = 进程内 ``cancel_event``（跨进程轮询通道留网关，契约冻结）；
  5. 内存定案 §8：输出完即清（累积器 reset）+ 无跨请求累积（实例只持配置
     句柄，per-request 状态在 execute 局部）+ 连接池无状态（llm_wire 单例）。

职责（§6.2 适配面）：消息组装 / conduit 流转发（on_chunk 钩子）/ 半截快照 /
取消传播 / tool_call id 修复（消费机内） / 方言路由与线材执行。

两个出口（§6.0 收敛图 / 批 5 生产改接）：
- ``route="wire"``（服务面缺省）：方言 wire 直发（连接池/SSE 解析在本进程）；
- ``route="gateway"``（生产管道面）：**网关出口** = 组装 ``llm.complete_stream``
  契约信封（tool_name/plugin_id/args/_call_context）经注入的网关句柄跨进程调用，
  由 llm_service 完成编排（KeyPool/兜底链/冷却/预算/归一化/模型解析单源）——
  管道面不再自己组装信封，也不再直连能力句柄（适配面→网关=跨进程契约调用，
  管道面→适配面=进程内调用，三线边界见主方案 §6.2）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import llm_dialects
from _config_models import ModelConfigLoaderShim, get_config
from llm_provider_adapters import resolve_replay_reasoning
from llm_stream import StreamCancelledError, WireStreamAdapter

logger = logging.getLogger(__name__)

# ── 出口路线键（两出口闭集）────────────────────────────────────────────
#: 方言 wire 直发（服务面 ``llm_adapter.execute`` 缺省语义）。
ROUTE_WIRE = "wire"
#: 网关出口（生产管道面）：``llm.complete_stream`` 唯一 llm 服务面（契约冻结）。
ROUTE_GATEWAY = "gateway"

#: 网关服务身份（契约冻结；生产管道路线恒定经它）。
_GATEWAY_TOOL_NAME = "llm.complete_stream"
_GATEWAY_PLUGIN_ID = "llm_service"
#: 网关等待上限余量（秒）：让 llm_service 的结构化错误信封先于 SDK 超时返回
#: （错误是值，不丢语义）。与 ``_capability_timeout`` 改接前口径一致。
_GATEWAY_TIMEOUT_MARGIN_S = 60.0
#: 模型不在配置表时的等待上限基准（与 ``get_llm_core_config`` 内部默认同口径）。
_GATEWAY_BASE_CALL_TIMEOUT_S = 300.0

# 内核错误链逐跳累积的传输包装（tool_executor「tool execution failed:」→
# invoker「MCP tool call failed:」→ mcp「tool call failed: <tool>:」「MCP
# protocol error: [N] 」），同一根因被多层前缀包裹。内核折叠文案 280 字
# 截断只保头部——不剥噪则用户只看到传输噪声、根因（含 LLM 供应商分类摘要）
# 被截掉。最后一跳「MCP protocol error: [N] 」之后即插件异常原文。
_MCP_PROTOCOL_MARKER = "MCP protocol error:"


def _strip_transport_noise(err: Any) -> Any:
    """剥离 MCP 错误链逐跳累积的传输前缀，保留根因尾部。

    取字符串中最后一处「MCP protocol error: [N] 」之后的原文（插件异常的
    str()，含 llm_service 分类摘要「LLM 供应商调用失败[认证失败]…」）；
    标记缺失（非 MCP 通道/格式漂移）或剥离结果为空时原样返回，不做猜测。
    """
    if not isinstance(err, str):
        return err
    idx = err.rfind(_MCP_PROTOCOL_MARKER)
    if idx < 0:
        return err
    tail = err[idx + len(_MCP_PROTOCOL_MARKER):].lstrip()
    if tail.startswith("["):
        end = tail.find("]")
        if end >= 0:
            tail = tail[end + 1:].lstrip()
    return tail or err


#: 网关出口句柄签名（与内核 capability caller 同形：``(method, params, timeout)``）。
GatewayRelay = Callable[[str, dict[str, Any], float | None], Awaitable[Any]]


@dataclass(frozen=True)
class GatewayContext:
    """网关调用上下文（管道 state 的路由/取消/优先级键 → 网关信封）。

    Attributes:
        agent_level: agent 层级（KeyPool 优先级排队读它；缺省 L3）。
        run_id: run 取消感知锚（内核注入；空 = 不携带该键）。
        thread_id: 流式事件路由键（session_id 优先，管道面已解析）。
        pipeline_id: 管道归属（流式事件/审批面）。
        message_id: 本轮消息 id（流式事件锚）。
    """

    agent_level: str = "L3"
    run_id: str = ""
    thread_id: str = ""
    pipeline_id: str = ""
    message_id: str = ""

#: finish reason 词汇收敛（复制锚点：源 C ``_FINISH_REASON_MAP`` @ c0fe6922fc）：
#: LLM 返回的未知结束原因（如 content_filter）映射 stop，消费端只按
#: stop/length/tool_calls/error/interrupted 五值渲染。
_FINISH_REASON_MAP: dict[str | None, str] = {
    None: "stop",
    "stop": "stop",
    "length": "length",
    "tool_calls": "tool_calls",
    "error": "error",
    "interrupted": "interrupted",
}

#: body 出站内部键黑名单（复制锚点：源 A ``_direct_call_with_slot`` 的 extra
#: 过滤清单 + 改造清单 3 的 max_thinking_chars + 本文件的适配面内部信封键）。
#: 这些键只服务调用编排，不属于任何 provider wire 契约。
_WIRE_INTERNAL_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "stream",
        "stream_options",
        "api_key",
        "api_base",
        "num_retries",
        "timeout",
        "first_chunk_timeout",
        "inter_chunk_timeout",
        "drop_params",
        "max_thinking_chars",
        "_wire_fallback_depth",
        "_plugin_fallback_depth",
        "_plugin_call_ctx",
        "_dialect_key",
        "_model_name",
        "on_chunk",
    }
)


class _PartialAccumulator:
    """流式部分内容累积器——异常/取消时产出半截快照（与网关侧同构）。

    复制锚点：源 B ``_PartialAccumulator`` @ c0fe6922fc。从消费机归一化 chunk
    （on_chunk 契约：``{"type","content"}`` 与 ``{"type": "tool_call",
    "tool_calls": [...]}``）累积 text/thinking/tool_calls；usage 由正常返回路径
    从响应补入（流中断时不可达，快照 usage 为 None——网关同语义）。
    改造：新增 ``reset()`` 供输出完即清（§8 条款 2）。
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
        """产出与网关同构的部分内容快照（usage 由调用方补）。"""
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

    def reset(self) -> None:
        """输出完即清（§8 条款 2）：释放累积缓冲，跨请求零保留。"""
        self._text_parts.clear()
        self._thinking_parts.clear()
        self._tool_calls.clear()


def _partial_result(
    stream_id: str,
    status: str,
    snapshot: dict[str, Any],
    dialect: str,
    exc: BaseException | None = None,
) -> dict[str, Any]:
    """组装中断/错误返回：partial 快照 + 语义字段（status/finish_reason）。

    复制锚点：源 B ``_partial_result`` @ c0fe6922fc；改造：增 ``dialect`` 诊断键。
    error 路径携带 ``llm_error_info``（错误类型与消息），interrupted 路径无错误
    信息（取消不是错误）。
    """
    result: dict[str, Any] = {
        "status": status,
        "stream_id": stream_id,
        "dialect": dialect,
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


@dataclass(frozen=True)
class _Resolved:
    """单次请求的配置解析结果（per-request 封闭，不落实例字段）。"""

    model_id: str
    model_name: str
    default_params: dict[str, Any]
    dialect: llm_dialects.Dialect
    api_base: str
    api_key: str
    replay_reasoning: Any


class _DialectWireAdapter(WireStreamAdapter):
    """适配面 wire 执行器：``_do_completion`` = 方言分派 + 发送（无编排面）。

    复制锚点：源 A ``KeyPoolAdapter._direct_call_with_slot`` 的 wire 发送段；
    编排面（KeyPool 选槽/冷却/兜底链/预算）不复制——留网关（§6.3）。
    """

    def __init__(self, config_provider: Callable[[], dict[str, Any]]) -> None:
        self._config_provider = config_provider

    async def _do_completion(self, **kwargs: Any) -> Any:
        """按方言构建 payload 并发送（stream/post 二选一）。

        kwargs 携带适配面内部信封（``api_base``/``api_key``/``_model_name``/
        ``_dialect_key``），由本方法弹出后再做 body 出站过滤。
        """
        kwargs.pop("drop_params", None)
        dialect_key = str(kwargs.pop("_dialect_key", "") or "")
        model_name = str(kwargs.pop("_model_name", "") or str(kwargs.get("model") or ""))
        api_base = str(kwargs.pop("api_base", "") or "")
        api_key = str(kwargs.pop("api_key", "") or "")
        model_id = str(kwargs.get("model") or "")
        if not api_base:
            raise RuntimeError(
                f"[llm_adapter] 模型 {model_id} 缺 api_base（models/provider 段均未提供，fail-closed）"
            )
        dialect = llm_dialects.get_dialect(dialect_key)
        extra = {k: v for k, v in kwargs.items() if k not in _WIRE_INTERNAL_KEYS}
        # body 构建含整份 json 序列化（大历史可达秒级）：出事件循环（源 A 同款）
        body = await asyncio.to_thread(
            dialect.build_payload,
            model_name,
            kwargs.get("messages") or [],
            tools=kwargs.get("tools"),
            stream=bool(kwargs.get("stream", True)),
            extra=extra,
        )
        try:
            timeout = float(kwargs.get("timeout") or kwargs.get("first_chunk_timeout") or 180.0)
        except (TypeError, ValueError):
            timeout = float(kwargs.get("first_chunk_timeout") or 180.0)
        if kwargs.get("stream", True):
            return await dialect.stream(api_base, api_key, body, timeout=timeout, model_label=model_id)
        return await dialect.post(api_base, api_key, body, timeout=timeout)


class LLMAdapterFace:
    """llm 适配服务面（同插件同进程；``llm_adapter.execute`` 的实现体）。

    生命周期（§6.2 三线）：实例只持只读配置句柄与无状态 wire 执行器；所有
    per-request 状态（累积器/解析结果）在 ``execute`` 局部，请求结束即清。
    """

    def __init__(self, config_provider: Callable[[], dict[str, Any]] | None = None) -> None:
        self._config_provider = config_provider or get_config
        self._adapter = _DialectWireAdapter(config_provider=self._config_provider)

    def _resolve(self, model: str, dialect: str) -> _Resolved:
        """模型/方言/端点/凭据解析（配置面读取；缺项 fail-closed）。"""
        loader = ModelConfigLoaderShim(self._config_provider() or {})
        model_id = loader.normalize_model_id(str(model or ""))
        entry = loader.get_model_config(model_id)
        if entry is None:
            raise RuntimeError(
                f"[llm_adapter] 模型 {model or model_id!r} 不在可用列表"
                "（llm.yaml models 未命中，fail-closed——不猜其他模型）"
            )
        provider = str(entry.get("provider") or "")
        conf = loader.get_provider_config(provider) or {}
        # 方言键 = providers.<name>.type（批 1 键位空间）；显式 dialect 参数优先
        impl = llm_dialects.get_dialect(str(dialect or conf.get("type") or ""))
        api_base = str(entry.get("api_base") or conf.get("api_base") or "")
        api_key = str(entry.get("api_key") or conf.get("api_key") or "")
        if not api_key:
            keys = conf.get("keys") or []
            if keys and isinstance(keys[0], dict):
                api_key = str(keys[0].get("api_key") or "")
        if "${" in api_key:
            # fail-closed：字面量 ${VAR} 发上游只会得到不可排查的 401（源 A 同纪律）
            raise RuntimeError(
                f"[llm_adapter] 模型 {model_id} 的 api_key 占位符未解析（{api_key}），"
                "发起 HTTP 前 fail-closed"
            )
        params = entry.get("default_params")
        return _Resolved(
            model_id=model_id,
            model_name=str(entry.get("model_name") or model_id),
            default_params=dict(params) if isinstance(params, dict) else {},
            dialect=impl,
            api_base=api_base,
            api_key=api_key,
            replay_reasoning=resolve_replay_reasoning(entry),
        )

    def _gateway_timeout(self, model_id: str) -> float:
        """网关等待上限：模型 call_timeout + 余量（入参是 model id 非 model_name）。

        SDK 默认 CAPABILITY_CALL_TIMEOUT_S=30s 面向短调用，LLM 耗时可到
        llm.yaml call_timeout——不传大值会先于 LLM 完成被掐断（事故形态：
        [-32001] tool-executor.invoke timed out，llm_usage 全 0）。模型不在
        配置表（热重载移除的在飞 run）走 300s 内部默认口径。
        """
        loader = ModelConfigLoaderShim(self._config_provider() or {})
        conf = loader.get_llm_core_config(model_id) if model_id else None
        call_timeout = float(conf["call_timeout"]) if conf else _GATEWAY_BASE_CALL_TIMEOUT_S
        return call_timeout + _GATEWAY_TIMEOUT_MARGIN_S

    async def _relay_via_gateway(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        params: dict[str, Any] | None,
        gateway: GatewayRelay | None,
        gateway_context: GatewayContext | None,
    ) -> dict[str, Any]:
        """网关出口：组装 ``llm.complete_stream`` 信封并经同宿主句柄调用。

        args 只含意图（``model`` = model id）+ 冻结参数 + 上下文键——端点/
        凭据不进入信封（连接解析单源归网关）。信封形状与批 3 前的管道面
        直连形态逐键一致（生产改接的行为回归基线）；错误信封剥传输噪声后
        fail-closed 上抛（错误是值，不以失败伪装空响应）。
        """
        if gateway is None:
            raise RuntimeError(
                "[llm_adapter] 网关出口未注入：gateway 路线只在同宿主进程内可用"
                "（生产管道面经 set_capability_caller 注入句柄后传入）"
            )
        context = gateway_context or GatewayContext()
        call_kwargs: dict[str, Any] = {"model": model, "messages": messages, **(params or {})}
        call_kwargs["agent_level"] = context.agent_level
        if context.run_id:
            call_kwargs["run_id"] = context.run_id
        if tools:
            call_kwargs["tools"] = tools
        # 流式事件路由键（_call_context）：llm_service 侧 _resolve_envelope 只读
        # arguments["_call_context"]；内核 tool-executor 把 params 级
        # _call_context 合入 args，故必须放 params 顶层（args 级同名键按内建键剥离）。
        envelope = {
            "tool_name": _GATEWAY_TOOL_NAME,
            "plugin_id": _GATEWAY_PLUGIN_ID,
            "args": call_kwargs,
            "_call_context": {
                "thread_id": context.thread_id,
                "pipeline_id": context.pipeline_id,
                "message_id": context.message_id,
            },
        }
        response = await gateway("invoke", envelope, self._gateway_timeout(model))
        # tool-executor.invoke 返回 {success, data, error} 信封（内核 ToolResult
        # 序列化）：success=false（工具未注册/执行失败）是错误不是空响应。
        if not isinstance(response, dict):
            raise RuntimeError(f"llm.complete_stream 信封形状异常: {type(response).__name__}")
        if not response.get("success"):
            raise RuntimeError(
                f"llm.complete_stream 工具执行失败: "
                f"{_strip_transport_noise(response.get('error') or response)}"
            )
        data = response.get("data")
        if not isinstance(data, dict):
            raise RuntimeError(f"llm.complete_stream 返回形状异常: {type(data).__name__}")
        return data

    async def execute(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = True,
        dialect: str = "",
        params: dict[str, Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        route: str = ROUTE_WIRE,
        gateway: GatewayRelay | None = None,
        gateway_context: GatewayContext | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """执行一次 LLM 调用（适配面唯一入口，两出口）。

        Args:
            model: 模型 id（llm.yaml models 段 key；model_name 形态经归一单点归一）。
            messages: 对话消息列表（调用方已完成管道侧组装）。
            tools: 工具 schema（可选）。
            stream: True=SSE 流式（生产路径）；False=非流式（仅 wire 路线）。
            dialect: 显式方言键（空=按 providers.<name>.type 解析；仅 wire 路线）。
            params: 采样/思考参数（显式优先，模型条目 default_params 兜底；
                内部超时键 first_chunk_timeout/inter_chunk_timeout 经消费机归一；
                gateway 路线原样进网关 kwargs，含 stream_idle_timeout 预算键）。
            cancel_event: 进程内停止信号（跨进程轮询通道留网关）。
            on_chunk: conduit 流转发钩子（归一 chunk 原样投递；返回 "stop" 截断）。
            route: ``wire``（方言 wire 直发，服务面缺省）/ ``gateway``（网关出口）。
            gateway: 网关出口句柄（仅进程内；gateway 路线必填）。
            gateway_context: 网关调用上下文（agent 层级/取消锚/事件路由键）。

        Returns:
            wire 路线：本文件 wire 执行结果（增 ``dialect`` 键）；gateway 路线：
            ``llm.complete_stream`` 聚合 dict 原样（partial 非 None = 中断/取消）。
        """
        kwargs.pop("_call_context", None)  # 内核路由信封：wire 路线无消费
        if route == ROUTE_GATEWAY:
            return await self._relay_via_gateway(
                model,
                messages,
                tools=tools,
                params=params,
                gateway=gateway,
                gateway_context=gateway_context,
            )
        if route != ROUTE_WIRE:
            raise ValueError(f"[llm_adapter] 未知路线 {route!r}（两出口闭集：wire|gateway）")
        stream_id = f"stream_{uuid.uuid4().hex}"
        resolved = self._resolve(model, dialect)
        accumulator = _PartialAccumulator()

        def _forward(chunk: dict[str, Any]) -> Any:
            accumulator.accumulate(chunk)
            if on_chunk is None:
                return None
            return on_chunk(chunk)

        call_kwargs: dict[str, Any] = {
            **resolved.default_params,
            **(params or {}),
            "model": resolved.model_id,
            "messages": messages,
            "tools": tools,
            "stream": stream,
            "on_chunk": _forward,
            "replay_reasoning": resolved.replay_reasoning,
            "cancel_event": cancel_event,
            "api_base": resolved.api_base,
            "api_key": resolved.api_key,
            "_model_name": resolved.model_name,
            "_dialect_key": resolved.dialect.key,
        }
        try:
            response = await self._adapter.completion(**call_kwargs)
        except StreamCancelledError:
            # 调用方停止（预期终止，不传播异常）：半截内容经 partial 交付
            return _partial_result(stream_id, "interrupted", accumulator.build_snapshot(), resolved.dialect.key)
        except BaseException as exc:
            # 任务级取消原样传播（不得转成业务返回）
            if isinstance(exc, asyncio.CancelledError):
                raise
            if accumulator.has_content():
                return _partial_result(
                    stream_id, "error", accumulator.build_snapshot(), resolved.dialect.key, exc=exc
                )
            raise
        finally:
            accumulator.reset()  # 输出完即清（§8 条款 2）

        return {
            "status": "streamed",
            "stream_id": stream_id,
            "dialect": resolved.dialect.key,
            "text": response.text,
            "tool_calls": response.tool_calls or [],
            "thinking_text": response.thinking_text,
            "usage": response.usage or {},
            "finish_reason": _FINISH_REASON_MAP.get(getattr(response, "finish_reason", None), "stop"),
            # 退化循环截断标记（消费方钩子返回 stop 时由消费机置位）
            "stream_repetition": bool(getattr(response, "stream_repetition", False)),
            # 正常完成：partial 恒为 None（半截快照只在异常/取消路径返回）
            "partial": None,
        }


@lru_cache(maxsize=1)
def get_face() -> LLMAdapterFace:
    """进程级适配面单例（服务入口；配置源 = _config_models 注入视图）。"""
    return LLMAdapterFace()


def reset_face() -> None:
    """清空适配面单例槽位（on_unload；幂等）。"""
    get_face.cache_clear()


async def execute(
    model: str,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    stream: bool = True,
    dialect: str = "",
    params: dict[str, Any] | None = None,
    route: str = ROUTE_WIRE,
    gateway: GatewayRelay | None = None,
    gateway_context: GatewayContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """服务入口（MCP tool ``llm_adapter.execute`` 的处理器体；生产管道面入口）。

    ``_call_context`` 等内核信封键经 kwargs 接收后忽略（适配面进程内语义）；
    ``route``/``gateway``/``gateway_context`` 为进程内出口选择与网关句柄，
    MCP 调用方缺省走 wire 路线。
    """
    return await get_face().execute(
        model,
        messages,
        tools=tools,
        stream=stream,
        dialect=dialect,
        params=params,
        route=route,
        gateway=gateway,
        gateway_context=gateway_context,
        **kwargs,
    )


__all__ = [
    "GatewayContext",
    "GatewayRelay",
    "LLMAdapterFace",
    "ROUTE_GATEWAY",
    "ROUTE_WIRE",
    "execute",
    "get_face",
    "reset_face",
]
