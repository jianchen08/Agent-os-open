"""
Mock LLM 客户端

用于测试和开发环境的模拟客户端
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from src.llm.base import LLMClient, LLMResponse, Message, TokenUsage, Tool, ToolCall

logger = logging.getLogger(__name__)


class MockClient(LLMClient):
    """Mock LLM 客户端 - 用于测试"""

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
        初始化 Mock 客户端

        Args:
            model_name: 模型名称
            api_key: API 密钥（Mock 不需要）
            api_base: API 基础 URL（Mock 不需要）
            default_params: 默认参数
            provider: 提供商名称（用于并发控制）
            enable_concurrency_control: 是否启用并发控制
        """
        detected_provider = provider or "mock"
        super().__init__(
            model_name,
            api_key,
            api_base,
            default_params,
            provider=detected_provider,
        )

    async def _generate_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> LLMResponse:
        """内部生成模拟响应实现"""
        logger.info(
            f"[Mock LLM] 生成响应 | model={self.model_name} | messages_count={len(messages)}"
        )

        # 模拟处理延迟
        await asyncio.sleep(0.1)

        # 获取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_message = msg.content or ""
                break

        # 生成模拟响应
        mock_response = self._generate_mock_response(user_message)

        return LLMResponse(
            content=mock_response,
            usage=TokenUsage(
                prompt_tokens=len(user_message.split()) if user_message else 0,
                completion_tokens=len(mock_response.split()),
                total_tokens=(
                    len(user_message.split()) + len(mock_response.split())
                    if user_message
                    else len(mock_response.split())
                ),
            ),
            model=self.model_name,
            finish_reason="stop",
        )

    async def _stream_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[str]:
        """内部流式生成模拟响应实现"""
        logger.info(
            f"[Mock LLM] 流式生成 | model={self.model_name} | messages_count={len(messages)}"
        )

        # 获取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_message = msg.content or ""
                break

        # 生成模拟响应
        mock_response = self._generate_mock_response(user_message)

        # 模拟流式输出
        words = mock_response.split()
        for word in words:
            await asyncio.sleep(0.05)  # 模拟延迟
            yield word + " "

    async def _generate_with_tools_internal(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs,
    ) -> LLMResponse:
        """内部带工具调用生成实现"""
        logger.info(
            f"[Mock LLM] 工具生成 | model={self.model_name} | tools_count={len(tools)}"
        )

        # 模拟处理延迟
        await asyncio.sleep(0.1)

        # 获取最后一条用户消息
        user_message = ""
        for msg in reversed(messages):
            if msg.role == "user":
                user_message = msg.content or ""
                break

        # 检查是否需要调用工具（简单的关键词匹配）
        tool_calls = []
        if any(  # noqa: SIM102
            keyword in user_message.lower()
            for keyword in ["文件", "读取", "写入", "搜索"]
        ):
            # 模拟工具调用
            if tools:
                tool = tools[0]  # 使用第一个工具
                tool_calls = [
                    ToolCall(
                        id="mock_tool_call_1",
                        name=tool.name,
                        arguments={"input": user_message},
                    )
                ]

        # 生成响应
        if tool_calls:
            mock_response = f"我需要调用 {tool_calls[0].name} 工具来处理您的请求。"
        else:
            mock_response = self._generate_mock_response(user_message)

        return LLMResponse(
            content=mock_response,
            tool_calls=tool_calls if tool_calls else None,
            usage=TokenUsage(
                prompt_tokens=len(user_message.split()) if user_message else 0,
                completion_tokens=len(mock_response.split()),
                total_tokens=(
                    len(user_message.split()) + len(mock_response.split())
                    if user_message
                    else len(mock_response.split())
                ),
            ),
            model=self.model_name,
            finish_reason="tool_calls" if tool_calls else "stop",
        )

    def _generate_mock_response(self, user_message: str) -> str:  # noqa: PLR0911
        """生成模拟响应内容"""
        if not user_message:
            return "您好！我是测试模型，请问有什么可以帮助您的吗？"

        # 简单的关键词响应
        user_lower = user_message.lower()

        if "你好" in user_lower or "hello" in user_lower:
            return "您好！我是测试模型，很高兴为您服务！"
        if "测试" in user_lower or "test" in user_lower:
            return "这是一个测试响应。测试模型工作正常！"
        if "帮助" in user_lower or "help" in user_lower:
            return "我是一个测试模型，可以帮助您测试系统功能。请告诉我您需要什么帮助。"
        if "代码" in user_lower or "code" in user_lower:
            return "我可以帮助您处理代码相关的问题。这是一个模拟的代码响应。"
        if "文件" in user_lower or "file" in user_lower:
            return "我可以帮助您处理文件操作。这是一个模拟的文件处理响应。"
        return f"我收到了您的消息：「{user_message}」。这是一个模拟响应，用于测试系统功能。"

    async def chat(self, message: str) -> str:
        """简单的聊天接口（兼容旧代码）"""
        messages = [Message(role="user", content=message)]
        response = await self.generate(messages)
        return response.content or ""
