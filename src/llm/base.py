"""LLM 客户端抽象基类。

定义统一的 LLM 调用接口（generate / stream / generate_with_tools）。
生产实现见 LiteLLMAdapter（src/llm/adapter.py），思考模型实现见
clients/reasoning.py。
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# 导入消息日志记录器
try:
    from src.llm.message_logger import get_message_logger

    _logger_import_success = True
except ImportError:

    def get_message_logger():
        return None

    _logger_import_success = False


class ToolCall(BaseModel):
    """工具调用"""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class TokenUsage(BaseModel):
    """Token 使用统计"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class Message(BaseModel):
    """消息"""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None  # 工具名称（tool 角色时使用）


class LLMResponse(BaseModel):
    """LLM 响应"""

    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str = "stop"


class Tool(BaseModel):
    """工具定义"""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


# ============================================
# LLM 客户端抽象基类
# ============================================


class LLMClient(ABC):
    """LLM 客户端抽象基类"""

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
        初始化 LLM 客户端

        Args:
            model_name: 模型名称
            api_key: API 密钥
            api_base: API 基础 URL
            default_params: 默认参数
            provider: 提供商名称（用于日志）
            enable_concurrency_control: 已废弃，并发控制由 KeyPool PrioritySemaphore 统一管理
        """
        self.model_name = model_name
        self.api_key = api_key
        self.api_base = api_base
        self.default_params = default_params or {}
        self.provider = provider or self._detect_provider()

    def _detect_provider(self) -> str:
        """
        从模型名称检测提供商

        Returns:
            提供商名称
        """
        # 默认实现，子类可以覆盖
        model_lower = self.model_name.lower()

        if any(x in model_lower for x in ["gpt", "openai"]):
            return "openai"
        elif any(x in model_lower for x in ["claude", "anthropic"]):
            return "anthropic"
        elif any(x in model_lower for x in ["glm", "zhipu", "智谱"]):
            return "zhipu"
        elif "ollama" in model_lower:
            return "ollama"
        else:
            return "openai"  # 默认

    @abstractmethod
    async def _generate_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> LLMResponse:
        """
        内部生成文本实现（子类实现）

        Args:
            messages: 消息列表
            **kwargs: 额外参数

        Returns:
            LLM 响应
        """

    @abstractmethod
    async def _stream_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[str]:
        """
        内部流式生成实现（子类实现）

        Args:
            messages: 消息列表
            **kwargs: 额外参数

        Yields:
            生成的文本片段
        """

    @abstractmethod
    async def _generate_with_tools_internal(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs,
    ) -> LLMResponse:
        """
        内部带工具调用生成实现（子类实现）

        Args:
            messages: 消息列表
            tools: 工具列表
            **kwargs: 额外参数

        Returns:
            LLM 响应（可能包含工具调用）
        """

    async def generate(
        self,
        messages: list[Message],
        task_id: str | None = None,
        session_id: str | None = None,
        check_budget: bool = True,
        **kwargs,
    ) -> LLMResponse:
        """
        生成文本（带并发控制和预算检查）

        Args:
            messages: 消息列表
            task_id: 任务 ID（用于预算追踪）
            session_id: 会话 ID（用于预算追踪）
            check_budget: 是否检查预算
            **kwargs: 额外参数

        Returns:
            LLM 响应

        Raises:
            BudgetExhaustedError: 预算耗尽时抛出
        """
        # 记录请求消息并获取请求ID
        msg_logger = get_message_logger()
        request_id: str | None = None
        logger.debug(
            f"generate() | msg_logger={msg_logger} | _logger_import_success={_logger_import_success}"
        )
        if msg_logger:
            logger.debug(f"generate() | 调用 log_request | model={self.model_name}")
            request_id = msg_logger.log_request(self.model_name, messages, **kwargs)
            logger.debug(f"generate() | log_request 完成 | request_id={request_id}")

        # 预算检查
        if check_budget:
            await self._check_budget_before_call(messages, task_id, session_id)

        # 执行生成（并发控制已由 KeyPool PrioritySemaphore 统一管理）
        try:
            response = await self._generate_internal(messages, **kwargs)
        except Exception as e:
            # 记录错误（包含请求ID）
            if msg_logger:
                msg_logger.log_error(
                    self.model_name,
                    e,
                    {"messages_count": len(messages)},
                    request_id=request_id,
                )
            raise

        # 记录响应消息
        if msg_logger:
            # 提取思考内容（如果存在）
            reasoning_content = getattr(response, "_reasoning_content", None)

            msg_logger.log_response(
                model=self.model_name,
                content=response.content,
                tool_calls=[tc.model_dump() for tc in response.tool_calls]
                if response.tool_calls
                else None,
                usage=response.usage.model_dump(),
                finish_reason=response.finish_reason,
                request_id=request_id,  # 关联请求ID
                reasoning_content=reasoning_content,  # 思考内容
            )

        # 记录用量
        if check_budget:
            await self._record_usage_after_call(response.usage, task_id, session_id)

        return response

    async def stream(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[str]:
        """
        流式生成（带并发控制）

        Args:
            messages: 消息列表
            **kwargs: 额外参数

        Yields:
            生成的文本片段
        """
        msg_logger = get_message_logger()

        # 记录流式请求开始
        if msg_logger:
            msg_logger.log_stream_start(self.model_name, messages, **kwargs)

        accumulated = []

        try:
            async for chunk in self._stream_internal(messages, **kwargs):
                accumulated.append(chunk)
                if msg_logger:
                    msg_logger.log_stream_chunk(
                        self.model_name, chunk, accumulated="".join(accumulated)
                    )
                yield chunk
        except Exception as e:
            # 记录错误
            if msg_logger:
                msg_logger.log_error(
                    self.model_name,
                    e,
                    {
                        "messages_count": len(messages),
                        "accumulated_length": len("".join(accumulated)),
                    },
                )
            raise

        # 记录流式响应完成
        if msg_logger:
            msg_logger.log_stream_end(
                model=self.model_name,
                full_content="".join(accumulated) if accumulated else None,
            )

    def _convert_tools_to_openai_format(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """将工具列表转换为 OpenAI 格式

        子类可以覆盖此方法以提供特定的转换逻辑。
        默认实现使用 Tool 对象的 attributes 直接构建。

        Args:
            tools: 工具列表

        Returns:
            OpenAI 格式的工具列表
        """
        openai_tools = []
        for tool in tools:
            # 获取参数 schema，优先使用 input_schema（原始定义）
            if hasattr(tool, "input_schema"):
                parameters = tool.input_schema
            elif hasattr(tool, "parameters"):
                parameters = tool.parameters
            else:
                parameters = {"type": "object", "properties": {}}

            # 确保 parameters 有基本结构
            if not isinstance(parameters, dict):
                parameters = {"type": "object", "properties": {}}
            if "type" not in parameters:
                parameters["type"] = "object"
            if "properties" not in parameters:
                parameters["properties"] = {}

            openai_tools.append({
                "type": "function",
                "function": {
                    "name": getattr(tool, "name", "unknown"),
                    "description": getattr(tool, "description", ""),
                    "parameters": parameters,
                },
            })
        return openai_tools

    async def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[Tool],
        task_id: str | None = None,
        session_id: str | None = None,
        check_budget: bool = True,
        **kwargs,
    ) -> LLMResponse:
        """
        带工具调用的生成（带并发控制和预算检查）

        Args:
            messages: 消息列表
            tools: 工具列表
            task_id: 任务 ID（用于预算追踪）
            session_id: 会话 ID（用于预算追踪）
            check_budget: 是否检查预算
            **kwargs: 额外参数

        Returns:
            LLM 响应（可能包含工具调用）

        Raises:
            BudgetExhaustedError: 预算耗尽时抛出
        """
        # 转换工具为 OpenAI 格式（用于日志记录，显示实际发送给大模型的格式）
        openai_tools = self._convert_tools_to_openai_format(tools)

        # 记录请求消息（包含工具定义）
        msg_logger = get_message_logger()
        request_id: str | None = None
        logger.debug(
            f"generate_with_tools() | msg_logger={msg_logger} | _logger_import_success={_logger_import_success}"
        )
        if msg_logger:
            logger.debug(
                f"generate_with_tools() | 调用 log_request | model={self.model_name} | tools_count={len(tools)}"
            )
            request_id = msg_logger.log_request(
                self.model_name, messages, tools=tools, openai_tools=openai_tools, **kwargs
            )
            logger.debug(
                f"generate_with_tools() | log_request 完成 | request_id={request_id}"
            )

        # 预算检查
        if check_budget:
            await self._check_budget_before_call(messages, task_id, session_id)

        # 执行生成（并发控制已由 KeyPool PrioritySemaphore 统一管理）
        try:
            response = await self._generate_with_tools_internal(
                messages, tools, **kwargs
            )
        except Exception as e:
            # 记录错误
            if msg_logger:
                msg_logger.log_error(
                    self.model_name,
                    e,
                    {"messages_count": len(messages), "tools_count": len(tools)},
                )
            raise

        # 记录响应消息
        if msg_logger:
            logger.debug(
                f"generate_with_tools() | 调用 log_response | request_id={request_id} | content_length={len(response.content) if response.content else 0}"
            )
            # 提取思考内容（如果存在）
            reasoning_content = getattr(response, "_reasoning_content", None)

            msg_logger.log_response(
                model=self.model_name,
                content=response.content,
                tool_calls=[tc.model_dump() for tc in response.tool_calls]
                if response.tool_calls
                else None,
                usage=response.usage.model_dump(),
                finish_reason=response.finish_reason,
                request_id=request_id,  # 关联请求ID
                reasoning_content=reasoning_content,  # 思考内容
            )
            logger.debug("generate_with_tools() | log_response 完成")

        # 记录用量
        if check_budget:
            await self._record_usage_after_call(response.usage, task_id, session_id)

        return response

    async def _check_budget_before_call(
        self,
        messages: list[Message],
        task_id: str | None,
        session_id: str | None,
    ) -> None:
        """调用前检查预算"""
        from src.core.exceptions import (
            BudgetExceededException,
            BudgetExhaustedError,
            QuotaExhaustedException,
        )
        from src.core.tokenizer import get_token_counter
        from src.cost_control.budget_manager import get_budget_manager

        # 估算 Token 数
        counter = get_token_counter()
        # 转换为字典格式
        messages_dict = [{"role": msg.role, "content": msg.content} for msg in messages]
        estimated_tokens = counter.count_messages(messages_dict, self.model_name)
        # 预留输出空间（假设输出是输入的 1.5 倍）
        estimated_tokens = int(estimated_tokens * 2.5)

        # 检查预算
        budget_manager = get_budget_manager()
        try:
            await budget_manager.check_budget(
                estimated_tokens,
                task_id=task_id,
                session_id=session_id,
            )
        except (BudgetExceededException, QuotaExhaustedException) as e:
            raise BudgetExhaustedError(
                message=str(e),
                remaining_tokens=0,
                usage_percent=getattr(e, "usage_percent", 100.0),
            )

    async def _record_usage_after_call(
        self,
        usage: TokenUsage,
        task_id: str | None,
        session_id: str | None,
    ) -> None:
        """调用后记录用量"""
        from src.cost_control.budget_manager import get_budget_manager

        budget_manager = get_budget_manager()
        await budget_manager.record_usage(
            tokens=usage.total_tokens,
            model=self.model_name,
            task_id=task_id,
            session_id=session_id,
        )

    def _merge_params(self, **kwargs) -> dict[str, Any]:
        """合并默认参数和传入参数"""
        params = self.default_params.copy()
        params.update(kwargs)
        return params
