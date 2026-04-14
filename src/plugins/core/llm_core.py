"""LLM Core 插件 — 基于 LiteLLM 的大模型调用实现。

通过 litellm.acompletion 调用大模型，支持流式回调。
重试由 PluginChain 的 error_policy 统一管理。

职责：
- 成功时输出 raw_result、raw_tool_calls，并将 assistant 回复 append 到 messages
- 失败时直接抛出异常，由 PluginChain 决定是否重试
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import litellm

from pipeline.plugin import ICorePlugin, PluginContext
from pipeline.types import ErrorPolicy, StateKeys

logger = logging.getLogger(__name__)


def _is_retryable_error(exc: Exception) -> bool:
    """判断异常是否可重试。

    检查异常是否为 LiteLLM 可重试类型（Timeout/ServiceUnavailable/
    RateLimit/APIConnection），同时兼容 Mock 场景。

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

    # 检查 isinstance（真实 litellm 异常）
    try:
        if isinstance(exc, (
            litellm.Timeout,
            litellm.ServiceUnavailableError,
            litellm.RateLimitError,
            litellm.APIConnectionError,
        )):
            return True
    except AttributeError:
        pass

    return False


class LLMCore(ICorePlugin):
    """LLM Core — LiteLLM 调用，流式回调。

    使用 litellm.acompletion 调用大模型。
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
    """

    error_policy = ErrorPolicy.RETRY
    max_retries: int = 3
    retry_delay: float = 1.0

    def __init__(self, config: dict[str, Any] | None = None) -> None:
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
            if streaming:
                result_text, tool_calls, thinking_text = await self._call_streaming(
                    messages, ctx, on_chunk
                )
            else:
                result_text, tool_calls, thinking_text = await self._call_completion(messages, ctx)

            logger.info(
                "[%s] LLM call succeeded (streaming=%s, thinking=%s)",
                self.name, streaming, bool(thinking_text),
            )

            # LLMCore 生产的 assistant 回复，由 LLMCore 负责 append 到 messages
            # 只追加对话历史部分（不含 system_message 和 dynamic_vars），
            # 因为 system_message 和 dynamic_vars 由 _build_messages() 每次重新组装
            history = list(ctx.state.get("messages", []))
            if tool_calls:
                # LLM 返回工具调用 → append assistant 消息（含 tool_calls）
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
                # LLM 普通文本回复 → append assistant 消息
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
        1. state["system_message"] — prompt_build 产出的 SystemMessage
        2. state["messages"] — 管道维护的对话历史（assistant + tool 回复等）
        3. state["prompt.dynamic_vars"] — 动态变量（追加在历史消息之后）

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
            messages.append({"role": "system", "content": dynamic_vars})

        logger.info(
            "[%s] _build_messages assembled %d messages | "
            "system=%s | history=%d | dynamic=%s",
            self.name, len(messages),
            bool(system_msg), len(history), bool(dynamic_vars),
        )
        if system_msg:
            content_preview = str(system_msg.get("content", ""))[:200]
            logger.debug("[%s] system_message preview: %s", self.name, content_preview)
        if dynamic_vars:
            logger.debug("[%s] dynamic_vars: %s", self.name, dynamic_vars)

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
        }
        provider_prefix = provider_map.get(self._provider, self._provider)
        return f"{provider_prefix}/{self._model}"

    async def _call_completion(
        self, messages: list[dict[str, Any]], ctx: PluginContext
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """非流式调用 LLM。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas

        Returns:
            (result_text, tool_calls) 元组：
            - result_text: LLM 响应文本
            - tool_calls: 解析后的工具调用列表
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
            kwargs["tools"] = tool_schemas

        response = await litellm.acompletion(**kwargs)

        choice = response.choices[0]
        result_text = choice.message.content
        tool_calls = self._parse_tool_calls(choice.message.tool_calls)

        # LiteLLM 统一将各提供商的推理内容映射到 message.reasoning_content
        thinking_text: str | None = None
        if hasattr(choice.message, 'reasoning_content') and choice.message.reasoning_content:
            thinking_text = choice.message.reasoning_content
            if not result_text:
                result_text = thinking_text
                logger.info("[%s] Used reasoning_content as result_text (len=%d)", self.name, len(result_text))

        return result_text, tool_calls, thinking_text

    async def _call_streaming(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> tuple[str | None, list[dict[str, Any]], str | None]:
        """流式调用 LLM。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            on_chunk: 流式回调函数，每个 chunk 调用一次

        Returns:
            (result_text, tool_calls, thinking_text) 元组：
            - result_text: LLM 响应完整文本（由 chunk 拼接）
            - tool_calls: 解析后的工具调用列表
            - thinking_text: 思考过程文本（如有）
        """
        kwargs: dict[str, Any] = {
            "model": self._get_model_string(),
            "messages": self._normalize_messages_for_provider(messages),
            "stream": True,
            **self._default_params,
        }
        if self._api_base:
            kwargs["api_base"] = self._api_base
        if self._api_key:
            kwargs["api_key"] = self._api_key

        # 从 state 读取工具 Schema（由 ToolSchemaPlugin 注入）
        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            kwargs["tools"] = tool_schemas

        response = await litellm.acompletion(**kwargs)

        # 收集流式 chunk
        result_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}

        async for chunk in response:
            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # LiteLLM 统一将各提供商的推理内容映射到 delta.reasoning_content
            # 包括 DeepSeek 原生字段、Anthropic thinking blocks、<think/> 标签解析等
            reasoning = getattr(delta, 'reasoning_content', None)
            if reasoning:
                thinking_parts.append(reasoning)
                if on_chunk:
                    on_chunk({"type": "thinking", "content": reasoning})

            # 文本内容（LiteLLM 已自动剥离 <think/> 标签中的内容）
            if delta.content:
                result_parts.append(delta.content)
                if on_chunk:
                    on_chunk({"type": "text", "content": delta.content})

            # 工具调用（流式增量）
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if hasattr(tc, "index") else 0
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "name": "",
                            "arguments": "",
                        }
                    if tc.function:
                        if tc.function.name:
                            tool_calls_map[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_map[idx]["arguments"] += tc.function.arguments

                if on_chunk:
                    on_chunk({"type": "tool_call", "tool_calls": delta.tool_calls})

        result_text = "".join(result_parts) if result_parts else None
        thinking_text = "".join(thinking_parts) if thinking_parts else None
        tool_calls = self._normalize_tool_calls(tool_calls_map)

        return result_text, tool_calls, thinking_text

    def _parse_tool_calls(
        self, raw_tool_calls: Any
    ) -> list[dict[str, Any]]:
        """解析非流式响应中的 tool_calls。

        Args:
            raw_tool_calls: LiteLLM 响应中的 tool_calls 对象列表

        Returns:
            标准化的工具调用列表 [{"name": ..., "arguments": ...}]
        """
        if not raw_tool_calls:
            return []

        parsed: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            parsed.append({
                "id": getattr(tc, "id", None) or f"call_{len(parsed)}",
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
        return parsed

    def _normalize_tool_calls(
        self, tool_calls_map: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """将流式收集的 tool_calls 映射归一化。

        Args:
            tool_calls_map: 索引到工具调用片段的映射

        Returns:
            排序后的标准化工具调用列表
        """
        if not tool_calls_map:
            return []

        result: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            result.append({
                "name": tc["name"],
                "arguments": tc["arguments"],
            })
        return result
