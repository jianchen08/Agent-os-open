"""LLM Core 插件 -- 基于 LLM Adapter 的大模型调用实现。

通过 LLM Adapter 中间层调用大模型，支持多模型 fallback 和流式回调。
重试由 PluginChain 的 error_policy 统一管理。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由 PluginChain 决定是否重试
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

from llm.adapter import FallbackAdapter, LiteLLMAdapter, LLMAdapter, LLMResponse
from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


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
    max_retries: int = 3
    retry_delay: float = 1.0

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        adapter: LLMAdapter | None = None,
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
                - fallback_models: 备用模型配置列表（用于构建 FallbackAdapter）
            adapter: 可选的外部注入适配器实例，若提供则忽略 config 中的 fallback 配置
        """
        self._config = config or {}
        self._provider: str = self._config.get("provider", "openai")
        self._model: str = self._config.get("model_name", "gpt-4")
        self._api_base: str | None = self._config.get("api_base")
        self._api_key: str | None = self._config.get("api_key")
        self._default_params: dict[str, Any] = self._config.get(
            "default_params", {"temperature": 0.7, "max_tokens": 4096}
        )
        # 允许配置覆盖类属性
        if "max_retries" in self._config:
            self.max_retries = self._config["max_retries"]
        if "retry_delay" in self._config:
            self.retry_delay = self._config["retry_delay"]

        # 构建适配器
        if adapter is not None:
            self._adapter = adapter
        else:
            self._adapter = self._build_adapter()

    def _build_adapter(self) -> LLMAdapter:
        """根据配置构建 LLM 适配器。

        如果配置中包含 fallback_models，则创建 FallbackAdapter，
        否则创建 LiteLLMAdapter。

        Returns:
            构建好的 LLMAdapter 实例
        """
        fallback_models = self._config.get("fallback_models")
        if fallback_models and isinstance(fallback_models, list):
            primary = LiteLLMAdapter()
            fallbacks: list[LLMAdapter] = [LiteLLMAdapter() for _ in fallback_models]
            logger.info(
                "[%s] 构建 FallbackAdapter，primary + %d 个 fallback",
                self.name, len(fallbacks),
            )
            return FallbackAdapter(primary=primary, fallbacks=fallbacks)

        return LiteLLMAdapter()

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "llm_core"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 50

    async def execute(self, ctx: PluginContext) -> dict[str, Any]:
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
        messages = self._build_messages(ctx.state)
        streaming = ctx.state.get("streaming", False)
        on_chunk: Callable[[dict[str, Any]], Any] | None = ctx.state.get("on_chunk")

        try:
            response: LLMResponse = await self._call_llm(
                messages, ctx, stream=streaming, on_chunk=on_chunk
            )

            result_text = response.text
            tool_calls = response.tool_calls
            thinking_text = response.thinking_text

            logger.info(
                "[%s] LLM call succeeded (streaming=%s, thinking=%s, text=%s, tool_calls=%d)",
                self.name, streaming, bool(thinking_text),
                (result_text or "")[:200], len(tool_calls or []),
            )
            if tool_calls:
                for tc in tool_calls:
                    logger.info(
                        "[%s] tool_call: %s(%s)",
                        self.name, tc.get("name", "?"),
                        str(tc.get("args", tc.get("arguments", "")))[:200],
                    )

            # LLMCore 生产的 assistant 回复，由 LLMCore 负责 append 到 messages
            # 只追加对话历史部分（不含 system_message 和 dynamic_vars），
            # 因为 system_message 和 dynamic_vars 由 _build_messages() 每次重新组装
            history = list(ctx.state.get("messages", []))
            if tool_calls:
                # LLM 返回工具调用 -> append assistant 消息（含 tool_calls）
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": result_text or "",
                    "tool_calls": [
                        {
                            "id": tc.get("id", f"call_{i}"),
                            "type": "function",
                            "function": {
                                "name": tc.get("name", ""),
                                "arguments": tc.get("args", tc.get("arguments", "")),
                            },
                        }
                        for i, tc in enumerate(tool_calls)
                    ],
                }
                history.append(assistant_msg)
            elif result_text:
                # LLM 普通文本回复 -> append assistant 消息
                history.append({"role": "assistant", "content": result_text})

            return {
                StateKeys.RAW_RESULT: result_text,
                StateKeys.RAW_ERROR: None,
                StateKeys.RAW_TOOL_CALLS: tool_calls,
                StateKeys.RAW_THINKING: thinking_text,
                "messages": history,
            }

        except Exception as exc:
            logger.error(
                "[%s] LLM call failed: %s — %s",
                self.name, type(exc).__name__, exc,
            )
            raise

    def _build_messages(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """从管道状态构建 LLM messages 列表。

        从三个来源组装：
        1. state["system_message"] -- prompt_build 产出的 SystemMessage
        2. state["messages"] -- 管道维护的对话历史（assistant + tool 回复等）
        3. state["prompt.dynamic_vars"] -- 动态变量（追加在历史消息之后）

        Args:
            state: 管道状态字典

        Returns:
            符合 OpenAI Chat API 格式的 messages 列表
        """
        messages: list[dict[str, Any]] = []

        # 1. SystemMessage（prompt_build 产出）
        system_msg = state.get("system_message")
        if system_msg:
            messages.append(system_msg)

        # 2. 历史消息（管道维护的对话历史）
        history = state.get("messages", [])
        messages.extend(history)

        # 3. 动态变量（追加在历史消息之后）
        dynamic_vars = state.get("prompt.dynamic_vars", "")
        if dynamic_vars:
            if self._provider == "minimax":
                if system_msg and messages:
                    messages[0]["content"] = messages[0].get("content", "") + "\n\n" + dynamic_vars
                else:
                    messages.insert(0, {"role": "system", "content": dynamic_vars})
            else:
                messages.append({"role": "system", "content": dynamic_vars})

        logger.info(
            "[%s] _build_messages assembled %d messages | "
            "system=%s | history=%d | dynamic=%s",
            self.name, len(messages),
            bool(system_msg), len(history), bool(dynamic_vars),
        )

        for idx, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")
            name = msg.get("name", "")
            tc_list = msg.get("tool_calls", [])
            prefix = f"[{self.name}] MSG-{idx} role={role}"
            if name:
                prefix += f" name={name}"
            if tc_list:
                logger.info("%s tool_calls=%s", prefix,
                            json.dumps(tc_list, ensure_ascii=False)[:1000] if tc_list else "[]")
            else:
                logger.info("%s content=%s", prefix, (str(content) or "")[:2000])

        return messages

    def _normalize_messages_for_provider(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """针对特定 LLM 提供商的消息格式修正。

        MiniMax API 要求 system 消息只能在第一位，非首位 system 消息
        会被转换为 user+name=system（与旧代码 MinimaxClient 行为一致）。

        Args:
            messages: 原始消息列表

        Returns:
            修正后的消息列表
        """
        if self._provider != "minimax":
            return messages

        converted_count = 0
        result = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "system" and idx > 0:
                converted_count += 1
                new_msg = dict(msg)
                new_msg["role"] = "user"
                new_msg["name"] = "system"
                result.append(new_msg)
            else:
                result.append(msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    args_str = fn.get("arguments", "")
                    if isinstance(args_str, str) and args_str:
                        try:
                            json.loads(args_str)
                        except (json.JSONDecodeError, TypeError):
                            logger.warning(
                                "[%s] MiniMax: assistant MSG-%d tool_call[%s] arguments 不是合法 JSON: %s",
                                self.name, idx, fn.get("name", "?"), args_str[:500],
                            )

        if converted_count:
            logger.info(
                "[%s] MiniMax: 将 %d 条非首位 system 消息转换为 user+name=system",
                self.name, converted_count,
            )

        return result

    def _get_model_string(self) -> str:
        """获取 LiteLLM 格式的模型标识字符串。

        LiteLLM 使用 "provider/model" 格式路由到不同的 LLM 提供商。

        Returns:
            LiteLLM 模型标识字符串
        """
        # 常见提供商映射
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

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> LLMResponse:
        """通过 adapter 调用 LLM。

        合并了原来的 _call_completion 和 _call_streaming，
        统一通过 adapter.completion() 处理。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            stream: 是否使用流式模式
            on_chunk: 流式回调函数

        Returns:
            统一的 LLMResponse 响应结构
        """
        kwargs: dict[str, Any] = {
            "model": self._get_model_string(),
            "messages": self._normalize_messages_for_provider(messages),
            **self._default_params,
        }
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key

        # 从 state 读取工具 Schema（由 ToolSchemaPlugin 注入）
        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            logger.info("[%s] tool_schemas count=%d | %s",
                        self.name, len(tool_schemas),
                        ", ".join(t.get("function", {}).get("name", "?") for t in tool_schemas))

        return await self._adapter.completion(
            model=kwargs.pop("model"),
            messages=kwargs.pop("messages"),
            tools=tool_schemas or None,
            stream=stream,
            on_chunk=on_chunk,
            **kwargs,
        )
