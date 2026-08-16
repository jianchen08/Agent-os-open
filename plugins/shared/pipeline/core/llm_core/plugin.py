"""LLM Core 插件 -- 基于 LLM Adapter 的大模型调用实现。

通过 LLM Adapter 中间层调用大模型，支持多模型 fallback 和流式回调。
重试由 PluginChain 的 error_policy 统一管理。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由 PluginChain 决定是否重试
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from _message_normalizer import (
    _is_valid_tool_call_id,
    normalize_messages_for_provider,
)
from _stream_repeat_monitor import StreamRepetitionMonitor
from adapter import (
    LiteLLMAdapter,
    LLMAdapter,
    LLMResponse,
)
from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)

# 日志中单条 content 最大长度，超过则截断
_LOG_CONTENT_MAX_LEN = 200

# ── 思考强度 → 模型参数（思考强度全链路）────────────────────────────
# 前端 user_input 携带 thinking_strength（off/low/medium/high），内核透传注入
# state；llm_core 在请求构造时按档位覆盖**思考相关参数**（reasoning_effort /
# thinking），与 default_params 合并后进 kwargs。reasoning_effort 经
# _provider_registry.apply_pre_send 对 openai/ 前缀 provider 自动挪进 extra_body
# 透传（DeepSeek 等）。off/缺失 → 不覆盖（保持 llm.yaml default_params 现状）。
#
# 决策（用户确认）：temperature / max_tokens 等采样参数**不随强度覆盖**——
# 强度只路由思考参数，采样参数始终用模型 default_params。
#
# 路由规则（模型配置优先）：模型可在 llm.yaml 的 models.<id>.thinking_strength_params
# 定义自己的强度→参数映射（不同模型思考参数不一致：DeepSeek reasoning_effort /
# MiniMax adaptive thinking / 无 reasoning 的普通模型）。模型配置了某档位 →
# 用模型配置；未配置的档位 → 回退内置默认表。内置表是各模型的兜底基线。
_THINKING_STRENGTH_PARAMS: dict[str, dict[str, Any]] = {
    "low": {"reasoning_effort": "low"},
    "medium": {"reasoning_effort": "medium"},
    "high": {"reasoning_effort": "high"},
}

# 思考强度允许覆盖的参数白名单：只含思考相关字段；
# temperature/max_tokens 等采样参数不随强度覆盖（即使用户配置里写了也过滤）。
_THINKING_STRENGTH_ALLOWED = {"reasoning_effort", "thinking"}


def resolve_thinking_strength_params(
    strength: str,
    model_params: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """思考强度 → 思考参数覆盖集；off/未知/空 → None（不覆盖）。

    Args:
        strength: 思考强度档位（off/low/medium/high）
        model_params: 模型级路由规则（llm.yaml models.<id>.thinking_strength_params），
            配置了该档位则优先使用（缺失字段用内置表补全）；None 或未配置该档位
            时回退内置默认表。仅白名单内思考参数生效（temperature/max_tokens 忽略）。
    """
    if not strength or strength == "off":
        return None
    if model_params and isinstance(model_params, dict):
        override = model_params.get(strength)
        if isinstance(override, dict):
            base = _THINKING_STRENGTH_PARAMS.get(strength, {})
            merged = {**base, **override}
        else:
            merged = _THINKING_STRENGTH_PARAMS.get(strength, {})
    else:
        merged = _THINKING_STRENGTH_PARAMS.get(strength, {})
    if not merged:
        return None
    return {k: v for k, v in merged.items() if k in _THINKING_STRENGTH_ALLOWED}


def _summarize_content_for_log(content: Any) -> str:
    """将消息内容转换为日志友好的简短摘要。

    处理多模态 content（list[dict]）和纯文本 content：
    - 纯文本：直接截断到 _LOG_CONTENT_MAX_LEN
    - 多模态列表：展示每个 block 的类型和摘要，base64 数据只保留前缀和长度

    Args:
        content: 消息内容，可能是 str 或 list[dict]

    Returns:
        截断后的日志字符串
    """
    if content is None:
        return ""
    if isinstance(content, str):
        if len(content) > _LOG_CONTENT_MAX_LEN:
            return f"{content[:_LOG_CONTENT_MAX_LEN]}...(truncated, total={len(content)})"
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block)[:_LOG_CONTENT_MAX_LEN])
                continue
            btype = block.get("type", "unknown")
            if btype == "text":
                text = str(block.get("text", ""))
                if len(text) > _LOG_CONTENT_MAX_LEN:
                    parts.append(f"{{text: {text[:_LOG_CONTENT_MAX_LEN]}...(truncated, len={len(text)})}}")
                else:
                    parts.append(f"{{text: {text}}}")
            elif btype == "image_url":
                url = (block.get("image_url") or {}).get("url", "")
                if url.startswith("data:"):
                    # base64 data URL：只记录 mime 和长度，不打印二进制内容
                    head = url.split(",", 1)[0]
                    parts.append(f"{{image_url: {head},<base64 len={len(url)}>}}")
                else:
                    parts.append(f"{{image_url: {url}}}")
            else:
                parts.append(f"{{{btype}}}")
        return "[" + ", ".join(parts) + "]"
    return str(content)[:_LOG_CONTENT_MAX_LEN]


def _is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试。

    检查异常是否为 LiteLLM 可重试类型（Timeout/ServiceUnavailable/
    RateLimit/APIConnection），同时兼容 Mock 场景。

    基于异常类名判断，不依赖 litellm 模块。

    Args:
        exc: 待检查的异常

    Returns:
        是否可重试
    """
    retryable_names = {
        "Timeout",
        "ServiceUnavailableError",
        "RateLimitError",
        "APIConnectionError",
    }
    # 检查异常类名是否匹配（兼容 Mock 场景）
    exc_type_name = type(exc).__name__
    if exc_type_name in retryable_names:
        return True

    # 检查异常链中是否包含可重试异常
    cause = exc.__cause__
    while cause:
        if type(cause).__name__ in retryable_names:
            return True
        cause = cause.__cause__

    return False


class LLMCore(ICorePlugin):
    """LLM Core -- LLM Adapter 调用，流式回调。

    通过 LLM Adapter 中间层调用大模型，支持多模型 fallback。
    成功时输出 raw_result 和 raw_tool_calls，并将 assistant 回复写入 messages。
    失败时直接抛出异常，由 PluginChain 的 error_policy 统一管理重试。

    Class Attributes:
        error_policy: 错误策略为 RETRY（由 PluginChain 统一管理）
        max_retries: 最大重试次数（供 PluginChain 使用）
        retry_delay: 首次重试延迟（秒）（供 PluginChain 使用）

    Attributes:
        _config: 插件配置字典，包含 provider/model/api_base/api_key 等
        _provider: 模型提供商（如 openai、minimax）
        _model: 模型标识（如 gpt-4、MiniMax-M2.7）
        _api_base: API 端点 URL
        _api_key: API 密钥
        _default_params: 默认调用参数（temperature、max_tokens 等）
        _adapter: LLM 调用适配器实例
    """

    error_policy = ErrorPolicy.RETRY
    max_retries: int = 1  # Router 已有 num_retries + fallback，Engine 层不重复重试
    retry_delay: float = 5.0
    overload_retry_delay: float = 60.0

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        adapter: LLMAdapter | None = None,
        router: Any | None = None,
    ) -> None:
        """初始化 LLM Core 插件。

        Args:
            config: 插件配置字典，支持以下键：
                - provider: 模型提供商（如 openai、minimax）
                - model_name: 模型标识（如 gpt-4、MiniMax-M2.7）
                - api_base: API 端点 URL
                - api_key: API 密钥
                - default_params: 默认调用参数（temperature、max_tokens 等）
                - max_retries: 最大重试次数（覆盖类属性）
                - retry_delay: 首次重试延迟秒数（覆盖类属性）
            adapter: 外部注入的适配器实例（如 KeyPoolAdapter），若未提供则创建 LiteLLMAdapter
        """
        self._config = config or {}
        self._provider: str = self._config.get("provider", "openai")
        # model_id（yaml key，如 deepseek-v4-pro-apigo）：路由标识，
        # 传给 Router/KeyPool 做 deployment 匹配，保证不同 provider 的同名模型隔离。
        # model_name（yaml 的 model_name，如 deepseek-v4-pro）：发给上游的真实模型名，
        # 直连模式拼 litellm model 字符串用。
        # 两者必须分开：model_name 重名时（官方与 apigo 同底模），靠 model_id 区分路由。
        self._model_id: str = self._config.get("model_id", "")
        self._model: str = self._config.get("model_name", "gpt-4")
        self._api_base: str | None = self._config.get("api_base")
        self._api_key: str | None = self._config.get("api_key")
        # 模型级思考强度路由规则（llm.yaml models.<id>.thinking_strength_params）：
        # 由 _apply_model_from_state 解析模型时更新；None = 未配置 → 用内置默认表。
        self._thinking_strength_params: dict[str, dict[str, Any]] | None = (
            self._config.get("thinking_strength_params")
        )
        self._context_window: int | None = self._config.get("context_window")
        if not self._context_window:
            logger.warning(
                "[%s] context_window 未配置！上下文守卫将无法工作。"
                " 请在模型配置（llm.yaml）或 core_plugins 中设置 context_window。"
                " model=%s, provider=%s",
                self.name,
                self._model,
                self._provider,
            )
        # default_params 兜底：空 dict = 不设采样参数，全走上游默认（=不设限），
        # 避免 reasoning model 被人为限 token。本字段是全链路唯一兜底点——
        # 配置层(models.py)与 resolver 均不覆盖空 dict，漏配模型最终落此。
        self._default_params: dict[str, Any] = self._config.get("default_params", {})
        self._call_timeout: float = float(self._config.get("call_timeout", 300))
        # 首 token 超时：首 chunk 不来时强制超时的秒数（默认 120s）。
        # 与 call_timeout（后续 chunk 超时）分离，因首字节卡死是高发场景。
        # 用 180s：覆盖 litellm.acompletion 内部建连 + 上游首字节全过程。
        # 实测 120s 偶发误判（上游慢节点），且 litellm 内部卡死时外层超时
        # 未必生效——adapter._direct_call_with_slot 就地再包一层 _await_with_escape
        # 兜底，此处 180s 作为该层的默认值，确保 litellm 这一行必然超时退出。
        self._first_token_timeout: float = float(self._config.get("first_token_timeout", 180))
        # 流式静默超时：连续 N 秒收不到任何 chunk 即中断死等（默认 600s）。
        # 与 call_timeout 分离：call_timeout 用于非流式整体超时，此处用于流式
        # inter-chunk 静默（每个 chunk 到达即重置，活跃推理不误触发）。
        self._stream_idle_timeout: float = float(self._config.get("stream_idle_timeout", 600))
        # 允许配置覆盖类属性
        if "max_retries" in self._config:
            self.max_retries = self._config["max_retries"]
        if "retry_delay" in self._config:
            self.retry_delay = self._config["retry_delay"]
        if "overload_retry_delay" in self._config:
            self.overload_retry_delay = self._config["overload_retry_delay"]

        # litellm 内置重试参数（透传给 litellm.acompletion，Router 模式下由 Router 管理）
        self._num_retries: int = self._config.get("num_retries", 3)
        self._retry_delay: float = self._config.get("retry_delay", 60.0)

        # 构建适配器
        if adapter is not None:
            self._adapter = adapter
            self._use_router = hasattr(adapter, "_router")
        else:
            self._adapter = LiteLLMAdapter()
            self._use_router = False

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    def _apply_model_from_state(self, state: dict[str, Any]) -> None:
        """从管道 state 动态解析本次调用的模型并更新 self（0.2 适配）。

        0.1 由 ``plugin_resolver.apply_agent_model_override`` 在管道启动前
        一次性覆盖 llm_call 实例；0.2 sidecar 无 plugin_resolver，改为
        execute 时从 state 读 model_id/model_tier 动态解析。

        优先级：``state.model_id`` > ``state.model_tier``(→ defaults.tiers)
        > ``llm.yaml defaults.chat``。任一命中即用其 model_id 查
        ``get_llm_core_config`` 更新 provider/model_name/api_base/api_key 等；
        全部缺失则保持构造时的默认配置不变（不阻断，由调用方降级）。

        Args:
            state: 管道状态字典，读 ``model_id`` / ``model_tier``。
        """
        # 已锁定同一 model_id 则跳过（避免每轮重复解析）
        resolved_id = state.get("model_id", "")
        if not resolved_id:
            tier = state.get("model_tier", "")
            if tier:
                resolved_id = self._resolve_tier(tier)
        if not resolved_id:
            resolved_id = self._default_chat_model()
        if not resolved_id or resolved_id == self._model_id:
            return

        llm_conf = self._get_llm_core_config(resolved_id)
        if not llm_conf:
            logger.warning(
                "[%s] model_id=%s 在 llm.yaml 未找到配置，保持当前 model=%s",
                self.name,
                resolved_id,
                self._model,
            )
            return

        self._model_id = resolved_id
        self._provider = llm_conf.get("provider", self._provider)
        self._model = llm_conf.get("model_name", self._model)
        self._api_base = llm_conf.get("api_base") or self._api_base
        self._api_key = llm_conf.get("api_key") or self._api_key
        self._context_window = llm_conf.get("context_window") or self._context_window
        if llm_conf.get("default_params"):
            self._default_params = llm_conf["default_params"]
        # 模型级思考强度路由规则（llm.yaml models.<id>.thinking_strength_params）：
        # 随模型解析更新；未配置 → None（_call_llm 回退内置默认表）。
        if "thinking_strength_params" in llm_conf:
            self._thinking_strength_params = llm_conf["thinking_strength_params"]
        logger.info(
            "[%s] model resolved: model_id=%s provider=%s model=%s",
            self.name,
            self._model_id,
            self._provider,
            self._model,
        )

    def _get_llm_core_config(self, model_id: str) -> dict[str, Any] | None:
        """通过 sidecar 注入的 config 桥取模型配置（与 0.1 对齐）。

        从 ``_config_models.get_model_config_loader()`` 拿 loader，调
        ``get_llm_core_config``。模型未配置时 loader 返回 None（合法降级）；
        import/loader 自身故障是 sidecar 接线 bug，直接抛出可见。
        """
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().get_llm_core_config(model_id)

    def _resolve_tier(self, tier: str) -> str:
        """tier → model_id（defaults.tiers 解析）。"""
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().resolve_tier(tier)

    def _default_chat_model(self) -> str:
        """defaults.chat 默认对话模型 id。"""
        from _config_models import get_model_config_loader  # noqa: PLC0415

        return get_model_config_loader().get_default_chat_model()

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """执行 LLM 调用，返回原始结果。

        调用 LLM 后，将 assistant 回复 append 到 messages 中。
        谁生产数据谁负责写入：LLMCore 生产的 assistant 回复，由 LLMCore 写入。

        失败时直接抛出异常，由 PluginChain 的 error_policy 统一管理重试。

        Args:
            ctx: 插件执行上下文

        Returns:
            核心执行结果字典，将合并到管道状态中

        Raises:
            Exception: LLM 调用失败时抛出异常
        """
        # 动态模型选择（0.2 适配）：每次 execute 从 state 读 model_id/model_tier，
        # 从注入的 llm.yaml 解析完整配置并更新 self。0.1 由 plugin_resolver 在
        # 管道启动前一次性覆盖 llm_call 实例；0.2 sidecar 无 plugin_resolver，
        # 改为 execute 时动态解析（支持多 agent/多模型切换）。
        # 优先级：state.model_id > state.model_tier(→ defaults.tiers) > defaults.chat
        self._apply_model_from_state(ctx.state)

        messages = self._build_messages(ctx.state)
        streaming = ctx.state.get("streaming", True)
        on_chunk: Callable[[dict[str, Any]], Any] | None = ctx.state.get("on_chunk")

        # 流式模式下包装 on_chunk，注入重复检测
        if streaming and on_chunk:
            on_chunk = StreamRepetitionMonitor(on_chunk)

        try:
            from key_pool import set_agent_priority  # noqa: PLC0415

            agent_level = ctx.state.get("agent_level", "L3")
            set_agent_priority(agent_level)

            response: LLMResponse = await self._call_llm(messages, ctx, stream=streaming, on_chunk=on_chunk)

            result_text = response.text
            tool_calls = response.tool_calls
            thinking_text = response.thinking_text
            # 输出截断信号：finish_reason=="length" 表示命中 max_tokens，
            # tool_call 的 arguments JSON 可能不完整。供下游识别截断、
            # 在写入结果中提示模型续写，避免留下半截文件。
            output_truncated = response.finish_reason == "length"

            # 流式重复检测：模型在流式输出中陷入重复循环
            if response.stream_repetition:
                logger.warning(
                    "[%s] 流式输出重复检测触发，丢弃重复内容并添加提醒",
                    self.name,
                )
                history = list(ctx.state.get("messages", []))
                history.append(
                    {
                        "role": "system",
                        "content": (
                            "[StreamRepetitionGuard] 检测到流式输出中出现重复内容，已截断。请重新组织输出，避免重复。"
                        ),
                    }
                )
                return {
                    StateKeys.RAW_RESULT: None,
                    StateKeys.RAW_ERROR: None,
                    StateKeys.RAW_TOOL_CALLS: [],
                    StateKeys.RAW_THINKING: None,
                    "messages": history,
                    "llm_usage": {},
                    "context_window": self._context_window,
                }

            # 思考内容过长检测：截断思考，丢弃本次输出，注入提示重新触发
            if response.thinking_truncated:
                retry_count = ctx.state.get("thinking_retry_count", 0) + 1
                max_retries = 3
                logger.warning(
                    "[%s] 思考内容过长已截断，丢弃本次输出，retry=%d/%d",
                    self.name,
                    retry_count,
                    max_retries,
                )
                history = list(ctx.state.get("messages", []))
                history.append(
                    {
                        "role": "system",
                        "content": (
                            "[ThinkingTruncationGuard] "
                            "上一轮思考内容过长已截断，本次输出已丢弃。"
                            "请直接给出结论或工具调用，不要冗长思考。"
                        ),
                    }
                )
                return {
                    StateKeys.RAW_RESULT: None,
                    StateKeys.RAW_ERROR: None,
                    StateKeys.RAW_TOOL_CALLS: [],
                    StateKeys.RAW_THINKING: None,
                    "messages": history,
                    "llm_usage": {},
                    "context_window": self._context_window,
                    "thinking_retry_needed": retry_count <= max_retries,
                    "thinking_retry_count": retry_count,
                }

            llm_usage = None
            if response.usage:
                llm_usage = {
                    "input_tokens": response.usage.get("prompt_tokens", 0),
                    "output_tokens": response.usage.get("completion_tokens", 0),
                    "total_tokens": response.usage.get("total_tokens", 0),
                    "cached_tokens": response.usage.get("cached_tokens", 0),
                }

            logger.info(
                "[%s] LLM call succeeded (streaming=%s, thinking=%s, text=%s, tool_calls=%d)",
                self.name,
                streaming,
                bool(thinking_text),
                (result_text or "")[:200],
                len(tool_calls or []),
            )
            # 完整响应记录到管道日志（DEBUG 级别）
            logger.debug(
                "[%s] LLM full response: text=%d chars, thinking=%d chars, usage=%s",
                self.name,
                len(result_text or ""),
                len(thinking_text or ""),
                llm_usage,
            )
            if tool_calls:
                for tc in tool_calls:
                    logger.debug(
                        "[%s] tool_call: %s(%s)",
                        self.name,
                        tc.get("name", "?"),
                        str(tc.get("args", tc.get("arguments", "")))[:200],
                    )
                    # 诊断：arguments repr，定位转义层级（adapter 返回时是否已双重转义）
                    _tc_args_raw = tc.get("args", tc.get("arguments", ""))
                    if isinstance(_tc_args_raw, str) and len(_tc_args_raw) > 100:
                        logger.debug(
                            "[%s] tool_call arguments repr前80: %s",
                            self.name,
                            repr(_tc_args_raw[:80]),
                        )

            # LLMCore 生产的 assistant 回复。新模型(op-based):只 emit 一个 append set op,
            # 由引擎 apply 到 state["messages"] + message_slots(不再返回全量 history)。
            appended_msg: dict[str, Any] | None = None
            if tool_calls:
                # 预先解析 tool_call_id，确保 assistant 消息和 state 中的 raw_tool_calls 使用一致的 id
                # 同时标准化 id 格式：部分模型返回非标准格式（如 call_function_xxx_1），
                # 统一替换为 call_<hex> 格式，确保系统内一致且 API 兼容
                resolved_ids: list[str] = []
                for tc in tool_calls:
                    raw_id = tc.get("id")
                    if raw_id and _is_valid_tool_call_id(raw_id):
                        resolved_ids.append(raw_id)
                    else:
                        std_id = f"call_{uuid.uuid4().hex[:24]}"
                        resolved_ids.append(std_id)
                        if raw_id:
                            logger.info(
                                "[%s] LLM 返回非标准 tool_call_id，已修正: %s → %s",
                                self.name,
                                raw_id,
                                std_id,
                            )

                # 将解析后的 id 回写到 raw_tool_calls，供后续 tool_core 使用
                for i, tc in enumerate(tool_calls):
                    tc["id"] = resolved_ids[i]

                # LLM 返回工具调用 -> append assistant 消息（含 tool_calls）
                # 统一保留 reasoning_content 到内存（不管 provider）：
                # 发送给 API 时由 ProviderAdapter 按 provider 决定是否剥离
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result_text or "",
                    "tool_calls": [
                        {
                            "id": resolved_ids[i],
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": tc.get("args", tc.get("arguments", "")),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
                if thinking_text:
                    assistant_msg["reasoning_content"] = thinking_text
                appended_msg = assistant_msg
            elif result_text:
                # LLM 普通文本回复 -> append assistant 消息
                _plain_msg: dict[str, Any] = {"role": "assistant", "content": result_text}
                if thinking_text:
                    _plain_msg["reasoning_content"] = thinking_text
                appended_msg = _plain_msg

            _pipeline_id = ctx.state.get("pipeline_id", "?")
            _iteration = ctx.state.get("iteration", -1)
            logger.debug(
                "[%s] pipeline=%s iter=%d LLM returned: text=%d chars, tool_calls=%d, thinking=%d chars",
                self.name,
                _pipeline_id,
                _iteration,
                len(result_text) if result_text else 0,
                len(tool_calls) if tool_calls else 0,
                len(thinking_text) if thinking_text else 0,
            )
            # 仅当产出了 assistant 消息才 emit append op（无文本/无工具调用时不追加）。
            messages_update = (
                {"_ops": [{"op": "set", "msg": appended_msg}]}
                if appended_msg is not None
                else None
            )
            result: dict[str, Any] = {
                StateKeys.RAW_RESULT: result_text,
                StateKeys.RAW_ERROR: None,
                StateKeys.RAW_TOOL_CALLS: tool_calls,
                StateKeys.RAW_THINKING: thinking_text,
                "llm_usage": llm_usage or {},
                "context_window": self._context_window,
                "llm_model": self._model,
                "llm_provider": self._provider,
                "llm_api_base": self._api_base,
                "output_truncated": output_truncated,
            }
            if messages_update is not None:
                result["messages"] = messages_update
            return result

        except Exception as exc:
            logger.error(
                "[%s] LLM call failed: %s — %s",
                self.name,
                type(exc).__name__,
                exc,
            )
            # 工具调用错误后重置消息配对缓存，确保下次全量扫描
            exc_msg = str(exc)
            if "tool_call" in exc_msg.lower() or "tool call" in exc_msg.lower():
                # 同目录平铺 import（与本文件 line 27 一致）。原 ``plugins.core...``
                # 路径不存在（plugins.core 包未定义），ImportError 会顶替原始异常上抛。
                from _message_normalizer import reset_pairing_cache  # noqa: PLC0415

                # 精确重置当前管道的缓存（pipeline_id 维度隔离后必须带 ID）
                _pipeline_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
                reset_pairing_cache(
                    self._provider,
                    self.name,
                    pipeline_id=_pipeline_id,
                )
                logger.info(
                    "[%s] 检测到 tool_call 相关错误，已重置配对缓存 (pipeline=%s)",
                    self.name,
                    _pipeline_id or "?",
                )
            raise

    def _build_messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """从管道状态构建 LLM messages 列表。

        拼接顺序：
        1. state["system_message"] -- prompt_build 产出的纯 SystemMessage
        2. state["compression_messages"] -- 压缩块独立消息（L2→L1→state_snapshot）
        3. state["messages"] -- 管道维护的对话历史（最近消息）
        4. state["multimodal_content"] -- 多模态内容（图片/文件等，合并到最后一条用户消息）
        5. state["prompt.dynamic_vars"] -- 动态变量（追加在最后）

        Args:
            state: 管道状态字典

        Returns:
            符合 OpenAI Chat API 格式的 messages 列表
        """
        messages: list[dict[str, Any]] = []

        # 1. SystemMessage（纯 prompt，永不变化 → cache hit）
        system_msg = state.get("system_message")
        if system_msg:
            messages.append(system_msg)

        # 2. 压缩消息（每个块独立消息，老→新。前缀匹配 → cache hit）
        #    _context_form 是语义标记内部字段（prompt_build 打标），发送前剥离，
        #    不进 API 载荷（同下方 history 段的 seq/tool_result 处理）。
        for cm in state.get("compression_messages", []):
            if "_context_form" in cm:
                cm = {k: v for k, v in cm.items() if k != "_context_form"}  # noqa: PLW2901
            messages.append(cm)

        # 3. 历史消息（管道维护的对话历史——压缩后只含最近消息）
        history = state.get("messages", [])
        for m in history:
            # 清理内部标记字段，不发给 LLM：
            # seq 槽位号（内核 apply 时带上，LLM 不需要）；
            # tool_result envelope 字段（tool 消息持久形态的一部分，发送前剥离，
            # 是 Rust 侧后续 envelope 搬家改动的前置配合）；
            # _context_form 语义标记（压缩优化任务 1：产出方打的内部字段，
            # 只供压缩链路消费，最终 LLM 载荷必须剥离以保持 cache 不变）。
            if "seq" in m or "tool_result" in m or "_context_form" in m:
                m = {k: v for k, v in m.items() if k not in ("seq", "tool_result", "_context_form")}  # noqa: PLW2901
            messages.append(m)

        # 4. 多模态内容（合并到最后一条用户消息）
        multimodal_content = state.get("multimodal_content")
        if multimodal_content and messages:
            # 找到最后一条用户消息
            for i in range(len(messages) - 1, -1, -1):
                if messages[i].get("role") == "user":
                    # 将纯文本内容转换为 content blocks 格式
                    existing_content = messages[i].get("content", "")
                    if isinstance(existing_content, str):
                        # 转换为 content blocks 数组
                        messages[i]["content"] = [{"type": "text", "text": existing_content}] + multimodal_content
                    elif isinstance(existing_content, list):
                        # 已经是 content blocks，直接追加
                        messages[i]["content"].extend(multimodal_content)
                    break

        # 5. 动态变量（每轮变化的上下文：时间戳、session_id 等）
        #    作为独立 user 消息追加在末尾，绝不合并进 messages[0]（system_message）。
        #    system_message 必须保持纯 prompt、永不变化（prompt cache 命中依赖此不变性），
        #    而 dynamic_vars 含时间戳等每轮变化的内容，合并进去会破坏 cache 并污染系统提示词。
        #    role 必须用 user：实测末尾的 role=system 且每轮变化的消息会让 DeepSeek prompt
        #    cache 缓存单元边界错位（命中率从 ~97% 崩到 ~5%），role=user 则正常（~99%）。
        #    内容用 <dynamic_vars> XML 包裹并标注"系统注入"，模型仍能识别为背景信息。
        dynamic_vars_msg = state.get("prompt.dynamic_vars")
        if dynamic_vars_msg:
            if isinstance(dynamic_vars_msg, dict):
                content = dynamic_vars_msg.get("content", "")
            else:
                content = str(dynamic_vars_msg)
            if content:
                messages.append(
                    {
                        "role": "user",
                        "content": content,
                    }
                )

        return messages

    def _writeback_cleaned_history(
        self,
        state: dict[str, Any],
        raw_messages: list[dict[str, Any]],
        cleaned_messages: list[dict[str, Any]],
    ) -> None:
        """把 normalize 清理后的历史段写回 state["messages"]。

        _build_messages 拼接顺序为 [system?] + compression* + history* + [dynamic_vars?]。
        normalize 的配对清理只发生在 history 段（移除孤儿 tool result /
        未配对 assistant(tool_calls)），不会删除 system/compression/dynamic_vars，
        因此前缀计数与后缀计数不变，可用偏移量定位历史段。

        Args:
            state: 管道状态字典
            raw_messages: normalize 前的完整消息列表
            cleaned_messages: normalize 后的完整消息列表
        """
        prefix_len = 0
        if state.get("system_message"):
            prefix_len += 1
        prefix_len += len(state.get("compression_messages", []))

        suffix_len = 1 if state.get("prompt.dynamic_vars") else 0

        raw_history_len = len(raw_messages) - prefix_len - suffix_len
        cleaned_history_len = len(cleaned_messages) - prefix_len - suffix_len
        if cleaned_history_len <= 0 or raw_history_len <= 0:
            return

        cleaned_history = cleaned_messages[prefix_len : prefix_len + cleaned_history_len]
        state["messages"] = list(cleaned_history)
        logger.info(
            "[%s] normalize 清理写回 state: history %d → %d 条（移除孤儿/未配对消息）",
            self.name,
            raw_history_len,
            cleaned_history_len,
        )

    def _get_model_string(self) -> str:
        """获取 LiteLLM 格式的模型标识字符串。

        LiteLLM 使用 "provider/model" 格式路由到不同的 LLM 提供商。

        优先用 router_factory 的动态映射（读 llm.yaml 的 providers.type 字段），
        命中自定义 provider（如 apigo → openai）。未命中（router 未初始化的测试
        场景）回退到内置常见提供商映射，保持兼容。

        Returns:
            LiteLLM 模型标识字符串
        """
        provider_prefix = ""
        try:
            from router_factory import get_litellm_prefix  # noqa: PLC0415

            provider_prefix = get_litellm_prefix(self._provider)
        except Exception:  # noqa: BLE001
            provider_prefix = ""

        # 动态映射未命中（空或返回原名）→ 回退到内置映射
        if not provider_prefix or provider_prefix == self._provider:
            provider_map = {
                "openai": "openai",
                "minimax": "minimax",
                "anthropic": "anthropic",
                "azure": "azure",
                "zhipu_coding": "zai",
                "zhipu": "zai",
            }
            provider_prefix = provider_map.get(self._provider, self._provider)
        return f"{provider_prefix}/{self._model}"

    async def _call_llm(  # noqa: PLR0912
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> LLMResponse:
        """通过 adapter 调用 LLM。

        Router 模式：用模型 ID 作为路由别名，不传 api_key/api_base/num_retries。
        直连模式：用完整的 "provider/model" 字符串，透传所有参数。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            stream: 是否使用流式模式
            on_chunk: 流式回调函数

        Returns:
            统一的 LLMResponse 响应结构
        """
        normalized_messages = normalize_messages_for_provider(
            messages,
            provider=self._provider,
            name=self.name,
            pipeline_id=ctx.state.get(StateKeys.PIPELINE_ID, ""),
        )

        if len(normalized_messages) < len(messages):
            self._writeback_cleaned_history(ctx.state, messages, normalized_messages)

        # 主动修复：Phase 1-4 转换后仍可能存在遗漏（极端边界情况），
        # 此处主动修复而非仅做诊断日志。
        if self._provider == "minimax":
            fix_count = 0
            for _i, _m in enumerate(normalized_messages):
                if _i > 0 and _m.get("role") == "system":
                    logger.warning(
                        "[%s] MiniMax 主动修复: 非首位 system→user idx=%d, content=%s",
                        self.name,
                        _i,
                        str(_m.get("content", ""))[:200],
                    )
                    _m["role"] = "user"
                    _m.pop("name", None)
                    fix_count += 1
            if fix_count:
                logger.warning(
                    "[%s] MiniMax 主动修复了 %d 条遗漏的 system 消息",
                    self.name,
                    fix_count,
                )

        logger.info(
            "[%s] Sending %d messages to LLM",
            self.name,
            len(normalized_messages),
        )

        for idx, msg in enumerate(normalized_messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            name = msg.get("name", "")
            tc_list = msg.get("tool_calls", [])
            prefix = f"[{self.name}] MSG-{idx} role={role}"
            if name:
                prefix += f" name={name}"
            if tc_list:
                try:
                    tc_str = json.dumps(tc_list, ensure_ascii=False, default=str)
                except (TypeError, ValueError):
                    tc_str = str(tc_list)
                logger.info(
                    "%s tool_calls=%s",
                    prefix,
                    tc_str if tc_list else "[]",
                )
            else:
                logger.info(
                    "%s content=%s",
                    prefix,
                    str(content) or "",
                )

        if self._use_router:
            # Router 路径：model 用 yaml key（model_id）做 deployment 匹配，
            # 保证 model_name 重名时（官方与 apigo 同底模）能路由到正确 provider。
            # _model_id 为空（旧 config）时回退到 _model，保持兼容。
            kwargs: dict[str, Any] = {
                "model": self._model_id or self._model,
                "messages": normalized_messages,
                **self._default_params,
            }
        else:
            # 直连路径：用完整 litellm 模型字符串，透传凭证和重试
            kwargs = {
                "model": self._get_model_string(),
                "messages": normalized_messages,
                **self._default_params,
            }
            if self._api_base:
                kwargs["api_base"] = self._api_base
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if self._num_retries:
                kwargs["num_retries"] = self._num_retries
                kwargs["retry_delay"] = self._retry_delay

        # 思考强度 → 模型参数覆盖：state.thinking_strength 非空（low/medium/high）
        # 时覆盖采样参数（与 default_params 合并）；off/缺失不覆盖（现状不变）。
        # 路由规则：模型级 thinking_strength_params 优先，未配置回退内置默认表。
        thinking_strength = str(ctx.state.get("thinking_strength") or "")
        strength_params = resolve_thinking_strength_params(
            thinking_strength, self._thinking_strength_params
        )
        if strength_params:
            kwargs.update(strength_params)
            logger.info(
                "[%s] thinking_strength=%s → params=%s",
                self.name,
                thinking_strength,
                strength_params,
            )

        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            logger.info(
                "[%s] tool_schemas count=%d | %s",
                self.name,
                len(tool_schemas),
                ", ".join(t.get("function", {}).get("name", "?") for t in tool_schemas),
            )

        # 调用前记录模型/API 信息
        model_str = self._model
        api_base = kwargs.get("api_base") or self._api_base or "default"
        logger.info(
            "[%s] Calling LLM: model=%s, provider=%s, api_base=%s, streaming=%s",
            self.name,
            model_str,
            self._provider,
            api_base,
            stream,
        )

        try:
            return await self._adapter.completion(
                model=kwargs.pop("model"),
                messages=kwargs.pop("messages"),
                tools=tool_schemas or None,
                stream=stream,
                on_chunk=on_chunk,
                inter_chunk_timeout=self._stream_idle_timeout,
                first_chunk_timeout=self._first_token_timeout,
                **kwargs,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] LLM call timed out (model=%s)",
                self.name,
                self._model if self._use_router else self._get_model_string(),
            )
            raise
