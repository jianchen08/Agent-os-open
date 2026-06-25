"""
Ollama 客户端

基于 LangChain ChatOllama 实现，支持本地运行的 Ollama 模型
"""

from collections.abc import AsyncIterator
from typing import Any

try:
    from langchain_ollama import ChatOllama
except ImportError:
    ChatOllama = None

from langchain_core.messages import AIMessage

from src.core.exceptions import LLMException as LLMError
from src.core.exceptions import ModelNotAvailableError
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


class OllamaClient(LLMClient):
    """Ollama 客户端 - 基于 LangChain ChatOllama"""

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,  # Ollama 不需要 API Key
        api_base: str | None = None,
        default_params: dict[str, Any] | None = None,
        provider: str | None = None,
        enable_concurrency_control: bool = True,
    ):
        """
        初始化 Ollama 客户端

        Args:
            model_name: 模型名称
            api_key: API 密钥（Ollama 不需要）
            api_base: API 基础 URL，默认 http://localhost:11434
            default_params: 默认参数
            provider: 提供商名称（用于并发控制）
            enable_concurrency_control: 是否启用并发控制
        """
        detected_provider = provider or "ollama"
        super().__init__(
            model_name,
            api_key,
            api_base,
            default_params,
            provider=detected_provider,
        )

        # Ollama 默认地址
        base_url = api_base or "http://localhost:11434"

        # 构建 ChatOllama 参数
        chat_params = {
            "model": model_name,
            "base_url": base_url,
        }

        # 合并默认参数
        if default_params:
            for key in ["temperature", "num_predict", "top_k", "top_p"]:
                if key in default_params:
                    chat_params[key] = default_params[key]
            # max_tokens 映射到 num_predict
            if "max_tokens" in default_params:
                chat_params["num_predict"] = default_params["max_tokens"]

        if ChatOllama is None:
            self._chat_model = None
        else:
            self._chat_model = ChatOllama(**chat_params)

    def _convert_tools(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """转换工具格式"""
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in tools
        ]

    def _parse_response(self, response: AIMessage, model: str = "") -> LLMResponse:
        """解析 LangChain AIMessage 为 LLMResponse"""
        tool_calls = None
        if response.tool_calls:
            tool_calls = [
                ToolCall(
                    id=tc.get("id", f"call_{i}"),
                    name=tc.get("name", ""),
                    arguments=tc.get("args", {}),
                )
                for i, tc in enumerate(response.tool_calls)
            ]

        # Ollama 的 usage 信息
        usage = TokenUsage()
        if hasattr(response, "response_metadata") and response.response_metadata:
            usage = TokenUsage(
                prompt_tokens=response.response_metadata.get("prompt_eval_count", 0),
                completion_tokens=response.response_metadata.get("eval_count", 0),
                total_tokens=response.response_metadata.get("prompt_eval_count", 0)
                + response.response_metadata.get("eval_count", 0),
            )

        return LLMResponse(
            content=response.content if isinstance(response.content, str) else "",
            tool_calls=tool_calls,
            usage=usage,
            model=model or self.model_name,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    def _handle_error(self, error: Exception) -> None:
        """处理错误"""
        error_str = str(error)

        if "404" in error_str or "not found" in error_str.lower():
            raise ModelNotAvailableError(self.model_name)
        elif "connection" in error_str.lower() or "connect" in error_str.lower():
            raise LLMError(
                f"无法连接到 Ollama 服务: {self.api_base or 'http://localhost:11434'}"
            )
        else:
            raise LLMError(f"Ollama 错误: {error_str}")

    async def _generate_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> LLMResponse:
        """内部生成文本实现"""
        if self._chat_model is None:
            raise LLMError("Ollama 客户端未正确初始化，请安装 langchain_ollama 包")

        params = self._merge_params(**kwargs)

        invoke_params = {}
        for key in ["temperature", "num_predict"]:
            if key in params:
                invoke_params[key] = params[key]
        if "max_tokens" in params:
            invoke_params["num_predict"] = params["max_tokens"]

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
        for key in ["temperature", "num_predict"]:
            if key in params:
                invoke_params[key] = params[key]
        if "max_tokens" in params:
            invoke_params["num_predict"] = params["max_tokens"]

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
        for key in ["temperature", "num_predict"]:
            if key in params:
                invoke_params[key] = params[key]
        if "max_tokens" in params:
            invoke_params["num_predict"] = params["max_tokens"]

        try:
            lc_messages = messages_to_langchain(messages)
            ollama_tools = self._convert_tools(tools)

            # 绑定工具
            model_with_tools = self._chat_model.bind(tools=ollama_tools)

            if invoke_params:
                model_with_tools = model_with_tools.bind(**invoke_params)

            response = await model_with_tools.ainvoke(lc_messages)
            return self._parse_response(response)
        except Exception as e:
            self._handle_error(e)

    def as_langchain(self) -> BaseChatModel:
        """获取底层 LangChain ChatOllama 实例"""
        return self._chat_model

    async def close(self):
        """关闭客户端（兼容旧接口）"""
        pass  # LangChain ChatOllama 不需要显式关闭

    # ============================================
    # 兼容旧接口的方法
    # ============================================

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """转换消息格式（兼容旧代码）"""
        result = []
        for msg in messages:
            item = {
                "role": msg.role,
                "content": msg.content or "",
            }
            result.append(item)
        return result
