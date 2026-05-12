"""
Anthropic 客户端

基于 LangChain ChatAnthropic 实现，支持 Claude 系列模型
"""

from collections.abc import AsyncIterator
from typing import Any

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import AIMessage

from src.core.exceptions import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
)
from src.core.exceptions import (
    LLMException as LLMError,
)
from src.llm.base import (
    BaseChatModel,
    LLMClient,
    LLMResponse,
    Message,
    TokenUsage,
    Tool,
    ToolCall,
    messages_to_langchain,
)


class AnthropicClient(LLMClient):
    """Anthropic 客户端 - 基于 LangChain ChatAnthropic"""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        default_params: dict[str, Any] | None = None,
        provider: str | None = None,
        enable_concurrency_control: bool = True,
    ):
        """
        初始化 Anthropic 客户端

        Args:
            model_name: 模型名称
            api_key: API 密钥
            api_base: API 基础 URL
            default_params: 默认参数
            provider: 提供商名称（用于并发控制）
            enable_concurrency_control: 是否启用并发控制
        """
        detected_provider = provider or "anthropic"
        super().__init__(
            model_name,
            api_key,
            api_base,
            default_params,
            provider=detected_provider,
            enable_concurrency_control=enable_concurrency_control,
        )

        # 构建 ChatAnthropic 参数
        chat_params = {
            "model": model_name,
        }

        if api_key:
            chat_params["api_key"] = api_key
        if api_base:
            chat_params["base_url"] = api_base

        # 设置默认 max_tokens（Anthropic 必需）
        default_max_tokens = 4096
        if default_params and "max_tokens" in default_params:
            default_max_tokens = default_params["max_tokens"]
        chat_params["max_tokens"] = default_max_tokens

        # 合并其他默认参数
        if default_params:
            for key in ["temperature", "timeout", "max_retries"]:
                if key in default_params:
                    chat_params[key] = default_params[key]

        self._chat_model = ChatAnthropic(**chat_params)

    def _convert_tools(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """转换工具格式为 Anthropic 格式"""
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in tools
        ]

    def _parse_response(self, response: AIMessage, model: str = "") -> LLMResponse:
        """解析 LangChain AIMessage 为 LLMResponse"""
        tool_calls = None
        if response.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("args", {}),
                )
                for tc in response.tool_calls
            ]

        # 从 response_metadata 获取 usage 信息
        usage = TokenUsage()
        if hasattr(response, "response_metadata") and response.response_metadata:
            usage_data = response.response_metadata.get("usage", {})
            usage = TokenUsage(
                prompt_tokens=usage_data.get("input_tokens", 0),
                completion_tokens=usage_data.get("output_tokens", 0),
                total_tokens=usage_data.get("input_tokens", 0)
                + usage_data.get("output_tokens", 0),
            )

        return LLMResponse(
            content=response.content if isinstance(response.content, str) else "",
            tool_calls=tool_calls,
            usage=usage,
            model=model or self.model_name,
            finish_reason=(
                response.response_metadata.get("stop_reason", "stop")
                if hasattr(response, "response_metadata") and response.response_metadata
                else "stop"
            ),
        )

    def _handle_error(self, error: Exception) -> None:
        """处理错误"""
        error_str = str(error)
        error_type = type(error).__name__

        if "rate_limit" in error_str.lower() or "RateLimit" in error_type:
            raise RateLimitError(error_str)
        elif "401" in error_str or "authentication" in error_str.lower():
            raise AuthenticationError(error_str)
        elif "400" in error_str or "invalid" in error_str.lower():
            raise InvalidRequestError(error_str)
        else:
            raise LLMError(error_str)

    async def _generate_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> LLMResponse:
        """内部生成文本实现"""
        params = self._merge_params(**kwargs)

        invoke_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        try:
            lc_messages = messages_to_langchain(messages)

            if invoke_params:
                model = self._chat_model.bind(**invoke_params)
                response = await model.ainvoke(lc_messages)
            else:
                response = await self._chat_model.ainvoke(lc_messages)

            return self._parse_response(response)
        except Exception as e:
            self._handle_error(e)

    async def _stream_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[str]:
        """内部流式生成实现"""
        params = self._merge_params(**kwargs)

        invoke_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        try:
            lc_messages = messages_to_langchain(messages)

            if invoke_params:
                model = self._chat_model.bind(**invoke_params)
            else:
                model = self._chat_model

            async for chunk in model.astream(lc_messages):
                if chunk.content:
                    yield chunk.content

        except Exception as e:
            self._handle_error(e)

    async def _generate_with_tools_internal(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs,
    ) -> LLMResponse:
        """内部带工具调用生成实现"""
        params = self._merge_params(**kwargs)

        invoke_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        try:
            lc_messages = messages_to_langchain(messages)
            anthropic_tools = self._convert_tools(tools)

            # 绑定工具
            model_with_tools = self._chat_model.bind(tools=anthropic_tools)

            if invoke_params:
                model_with_tools = model_with_tools.bind(**invoke_params)

            response = await model_with_tools.ainvoke(lc_messages)
            return self._parse_response(response)
        except Exception as e:
            self._handle_error(e)

    def as_langchain(self) -> BaseChatModel:
        """获取底层 LangChain ChatAnthropic 实例"""
        return self._chat_model

    # ============================================
    # 兼容旧接口的方法
    # ============================================

    def _convert_messages(
        self, messages: list[Message]
    ) -> tuple[str | None, list[dict]]:
        """
        转换消息格式（兼容旧代码）

        Returns:
            (system_prompt, messages)
        """
        system_prompt = None
        result = []

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
                continue

            content = []

            if msg.content:
                content.append({"type": "text", "text": msg.content})

            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )

            if msg.tool_call_id:
                content = [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.tool_call_id,
                        "content": msg.content or "",
                    }
                ]

            result.append(
                {
                    "role": "user" if msg.role == "user" else "assistant",
                    "content": (
                        content
                        if len(content) > 1
                        else (content[0] if content else {"type": "text", "text": ""})
                    ),
                }
            )

        return system_prompt, result
