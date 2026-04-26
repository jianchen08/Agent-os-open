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
    overload_retry_delay: float = 180.0

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
        self._context_window: int | None = self._config.get("context_window")
        self._default_params: dict[str, Any] = self._config.get(
            "default_params", {"temperature": 0.7, "max_tokens": 4096}
        )
        # LLM 调用超时（秒）
        self._call_timeout: int = self._config.get("call_timeout", 300)

        # 允许配置覆盖类属性
        if "max_retries" in self._config:
            self.max_retries = self._config["max_retries"]
        if "retry_delay" in self._config:
            self.retry_delay = self._config["retry_delay"]
        if "overload_retry_delay" in self._config:
            self.overload_retry_delay = self._config["overload_retry_delay"]

        # litellm 内置重试参数（透传给 litellm.acompletion）
        self._num_retries: int = self._config.get("num_retries", 3)
        self._retry_delay: float = self._config.get("retry_delay", 60.0)

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

            llm_usage = None
            if response.usage:
                llm_usage = {
                    "input_tokens": response.usage.get("prompt_tokens", 0),
                    "output_tokens": response.usage.get("completion_tokens", 0),
                    "total_tokens": response.usage.get("total_tokens", 0),
                }

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
                "llm_usage": llm_usage or {},
                "context_window": self._context_window,
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

        # 3. 动态变量（每轮变化的上下文：时间戳、session_id 等）
        #    不修改系统消息（动态变量每轮变化，不应污染静态系统提示）
        #    使用 user 角色 + name=dynamic_context，兼容所有 provider
        dynamic_vars = state.get("prompt.dynamic_vars", "")
        if dynamic_vars:
            messages.append({
                "role": "user",
                "name": "dynamic_context",
                "content": dynamic_vars,
            })

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
                            json.dumps(tc_list, ensure_ascii=False) if tc_list else "[]")
            else:
                logger.info("%s content=%s", prefix, str(content) or "")

        return messages

    def _normalize_messages_for_provider(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """针对特定 LLM 提供商的消息格式修正。

        MiniMax API 要求：
        1. system 消息只能在第一位，非首位 system 消息转为 user+name=system
        2. assistant(tool_calls) 后只能紧跟 tool 消息，中间插入的非 tool
           消息（如 TaskReminder 注入的 system/user）需移到 tool 消息组之后

        Args:
            messages: 原始消息列表

        Returns:
            修正后的消息列表
        """
        if self._provider != "minimax":
            return messages

        converted_count = 0
        relocated_count = 0

        # Phase 1: 标准转换（system→user, tool 内容清理）
        # MiniMax 要求所有 user 消息的 name 字段一致，因此统一不设置 name
        converted: list[dict[str, Any]] = []
        for idx, msg in enumerate(messages):
            if msg.get("role") == "system" and idx > 0:
                converted_count += 1
                new_msg = dict(msg)
                new_msg["role"] = "user"
                new_msg.pop("name", None)
                converted.append(new_msg)
            elif msg.get("role") == "user" and msg.get("name"):
                new_msg = dict(msg)
                new_msg.pop("name", None)
                converted.append(new_msg)
            elif msg.get("role") == "tool":
                new_msg = dict(msg)
                content = new_msg.get("content", "")
                if isinstance(content, str):
                    content = content.replace("\x00", "")
                    if len(content) > 8000:
                        content = content[:8000] + "\n...[truncated]"
                    new_msg["content"] = content
                converted.append(new_msg)
            else:
                converted.append(msg)

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

        # Phase 2: 重定位 assistant(tool_calls) 和 tool 之间的非法消息
        result: list[dict[str, Any]] = []
        i = 0
        while i < len(converted):
            msg = converted[i]
            result.append(msg)

            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                # 收集紧随其后的 tool 消息
                tool_group: list[dict[str, Any]] = []
                intruders: list[dict[str, Any]] = []
                j = i + 1
                while j < len(converted):
                    if converted[j].get("role") == "tool":
                        tool_group.append(converted[j])
                        j += 1
                    elif tool_group:
                        # 已经有 tool 消息了，后续非 tool 消息是新的对话轮次，停止
                        break
                    else:
                        # assistant(tool_calls) 后第一个消息不是 tool → 非法插入
                        intruders.append(converted[j])
                        j += 1
                if intruders:
                    relocated_count += len(intruders)
                    result.extend(tool_group)
                    # 将非法消息转为 user 角色放在 tool 组之后
                    for intr in intruders:
                        if intr.get("role") not in ("user", "tool"):
                            moved = dict(intr)
                            moved["role"] = "user"
                            moved["name"] = intr.get("role", "system")
                            result.append(moved)
                        else:
                            result.append(intr)
                    i = j
                    continue
                elif tool_group:
                    result.extend(tool_group)
                    i = j
                    continue
            i += 1

        if converted_count:
            logger.info(
                "[%s] MiniMax: 将 %d 条非首位 system 消息转换为 user+name=system",
                self.name, converted_count,
            )
        if relocated_count:
            logger.info(
                "[%s] MiniMax: 重定位 %d 条 assistant(tool_calls) 与 tool 之间的非法消息",
                self.name, relocated_count,
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

    _LLM_CALL_TIMEOUT = 300

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        ctx: PluginContext,
        *,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
    ) -> LLMResponse:
        """通过 adapter 调用 LLM，带超时保护。

        合并了原来的 _call_completion 和 _call_streaming，
        统一通过 adapter.completion() 处理。
        使用 asyncio.wait_for 添加超时，防止 LLM API 无响应时 pipeline 永久挂起。

        Args:
            messages: 对话消息列表
            ctx: 插件执行上下文，用于读取 tool_schemas
            stream: 是否使用流式模式
            on_chunk: 流式回调函数

        Returns:
            统一的 LLMResponse 响应结构

        Raises:
            asyncio.TimeoutError: LLM 调用超时时抛出
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
        if self._num_retries:
            kwargs["num_retries"] = self._num_retries
            kwargs["retry_delay"] = self._retry_delay

        tool_schemas = ctx.state.get("tool_schemas", [])
        if tool_schemas:
            logger.info("[%s] tool_schemas count=%d | %s",
                        self.name, len(tool_schemas),
                        ", ".join(t.get("function", {}).get("name", "?") for t in tool_schemas))

        timeout = ctx.state.get("llm_call_timeout", self._call_timeout)
        try:
            return await asyncio.wait_for(
                self._adapter.completion(
                    model=kwargs.pop("model"),
                    messages=kwargs.pop("messages"),
                    tools=tool_schemas or None,
                    stream=stream,
                    on_chunk=on_chunk,
                    **kwargs,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] LLM call timed out after %ds (model=%s)",
                self.name, timeout, self._get_model_string(),
            )
            raise
