"""
思考模型 LangChain 适配器

将思考模型客户端适配为 LangChain BaseChatModel
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, PrivateAttr

logger = logging.getLogger(__name__)


class ReasoningLangChainAdapter(BaseChatModel):
    """思考模型 LangChain 适配器"""

    # 使用 PrivateAttr 存储非 Pydantic 字段
    _reasoning_client: Any = PrivateAttr(default=None)
    _bound_tools: list[Any] = PrivateAttr(default_factory=list)

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        extra="allow",
    )

    def __init__(self, reasoning_client, **kwargs):
        """
        初始化适配器

        Args:
            reasoning_client: 思考模型客户端实例
        """
        super().__init__(**kwargs)
        self._reasoning_client = reasoning_client

    @property
    def _llm_type(self) -> str:
        """返回 LLM 类型"""
        return f"reasoning_{self._reasoning_client.reasoning_type}"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        生成聊天响应

        Args:
            messages: 消息列表
            stop: 停止词列表
            run_manager: 回调管理器
            **kwargs: 额外参数

        Returns:
            聊天结果
        """
        try:
            # 转换消息格式
            formatted_messages = self._format_messages(messages)

            # 添加停止词
            if stop:
                kwargs["stop"] = stop

            # 如果有绑定工具，转换为 API 格式
            if self._bound_tools:
                kwargs["tools"] = self._convert_tools(self._bound_tools)
                logger.debug(
                    f"[ReasoningLangChainAdapter] 传递工具参数 | 工具数={len(self._bound_tools)}"
                )

            # 调用思考模型客户端
            import asyncio

            response = asyncio.run(
                self._reasoning_client.generate(formatted_messages, **kwargs)
            )

            # 构建响应消息
            ai_message = AIMessage(content=response.content)

            # 添加工具调用信息
            if response.tool_calls:
                tool_calls_data = []
                for tc in response.tool_calls:
                    args_raw = tc.arguments
                    if isinstance(args_raw, str):
                        try:
                            args_dict = json.loads(args_raw)
                            logger.debug(
                                f"[ReasoningLangChainAdapter] 转换 arguments JSON 字符串 | 工具={tc.name}"
                            )
                        except json.JSONDecodeError as e:
                            logger.error(
                                f"[ReasoningLangChainAdapter] arguments JSON 解析失败: {e} | 工具={tc.name} | raw={args_raw[:100]}"
                            )
                            args_dict = {}
                    elif isinstance(args_raw, dict):
                        args_dict = args_raw
                    else:
                        logger.warning(
                            f"[ReasoningLangChainAdapter] arguments 类型异常: {type(args_raw)} | 工具={tc.name} | 使用空字典"
                        )
                        args_dict = {}
                    tool_calls_data.append(
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "args": args_dict,
                        }
                    )
                # LangChain 的 AIMessage 在初始化时不能设置 tool_calls
                # 需要通过 additional_kwargs 或直接设置属性
                # 这里使用 setattr 方式
                ai_message.tool_calls = tool_calls_data
                logger.debug(
                    f"[ReasoningLangChainAdapter] 添加工具调用 | "
                    f"数量={len(tool_calls_data)}"
                )

            # 添加思考过程到元数据
            # 检查对象属性（reasoning_client 可能将
            # reasoning_content 存储在属性中）
            reasoning_content = getattr(response, "_reasoning_content", None)
            if reasoning_content:
                ai_message.additional_kwargs["reasoning_content"] = reasoning_content

            # 兼容字典格式（如果未来改为返回字典）
            if hasattr(response, "__contains__") and "reasoning_content" in response:
                ai_message.additional_kwargs["reasoning_content"] = response[
                    "reasoning_content"
                ]
            if hasattr(response, "__contains__") and "thinking_process" in response:
                ai_message.additional_kwargs["thinking_process"] = response[
                    "thinking_process"
                ]

            generation = ChatGeneration(message=ai_message)

            # 添加使用统计
            llm_output = {}
            if hasattr(response, "usage") and response.usage:
                llm_output["token_usage"] = response.usage.model_dump()
            if hasattr(response, "model") and response.model:
                llm_output["model_name"] = response.model

            return ChatResult(generations=[generation], llm_output=llm_output)

        except Exception as e:
            logger.error(f"思考模型适配器生成失败: {e}")
            # 返回错误消息
            error_message = AIMessage(content=f"思考模型生成失败: {str(e)}")
            generation = ChatGeneration(message=error_message)
            return ChatResult(generations=[generation])

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        异步生成聊天响应

        Args:
            messages: 消息列表
            stop: 停止词列表
            run_manager: 回调管理器
            **kwargs: 额外参数

        Returns:
            聊天结果
        """
        try:
            # 转换消息格式
            formatted_messages = self._format_messages(messages)

            # 添加停止词
            if stop:
                kwargs["stop"] = stop

            # 如果有绑定工具，转换为 API 格式
            if self._bound_tools:
                kwargs["tools"] = self._convert_tools(self._bound_tools)
                logger.debug(
                    f"[ReasoningLangChainAdapter] 传递工具参数 | 工具数={len(self._bound_tools)}"
                )

            # 调用思考模型客户端
            response = await self._reasoning_client.generate(
                formatted_messages, **kwargs
            )

            # 构建响应消息
            ai_message = AIMessage(content=response.content)

            # 添加工具调用信息
            if response.tool_calls:
                tool_calls_data = []
                for tc in response.tool_calls:
                    args_raw = tc.arguments
                    if isinstance(args_raw, str):
                        try:
                            args_dict = json.loads(args_raw)
                            logger.debug(
                                f"[ReasoningLangChainAdapter] 转换 arguments JSON 字符串 | 工具={tc.name}"
                            )
                        except json.JSONDecodeError as e:
                            logger.error(
                                f"[ReasoningLangChainAdapter] arguments JSON 解析失败: {e} | 工具={tc.name} | raw={args_raw[:100]}"
                            )
                            args_dict = {}
                    elif isinstance(args_raw, dict):
                        args_dict = args_raw
                    else:
                        logger.warning(
                            f"[ReasoningLangChainAdapter] arguments 类型异常: {type(args_raw)} | 工具={tc.name} | 使用空字典"
                        )
                        args_dict = {}
                    tool_calls_data.append(
                        {
                            "id": tc.id,
                            "name": tc.name,
                            "args": args_dict,
                        }
                    )
                # LangChain 的 AIMessage 在初始化时不能设置 tool_calls
                # 需要通过 additional_kwargs 或直接设置属性
                # 这里使用 setattr 方式
                ai_message.tool_calls = tool_calls_data
                logger.debug(
                    f"[ReasoningLangChainAdapter] 添加工具调用 | "
                    f"数量={len(tool_calls_data)}"
                )

            # 添加思考过程到元数据
            # 检查对象属性（reasoning_client 可能将
            # reasoning_content 存储在属性中）
            reasoning_content = getattr(response, "_reasoning_content", None)
            if reasoning_content:
                ai_message.additional_kwargs["reasoning_content"] = reasoning_content

            # 兼容字典格式（如果未来改为返回字典）
            if hasattr(response, "__contains__") and "reasoning_content" in response:
                ai_message.additional_kwargs["reasoning_content"] = response[
                    "reasoning_content"
                ]
            if hasattr(response, "__contains__") and "thinking_process" in response:
                ai_message.additional_kwargs["thinking_process"] = response[
                    "thinking_process"
                ]

            generation = ChatGeneration(message=ai_message)

            # 添加使用统计
            llm_output = {}
            if hasattr(response, "usage") and response.usage:
                llm_output["token_usage"] = response.usage.model_dump()
            if hasattr(response, "model") and response.model:
                llm_output["model_name"] = response.model

            return ChatResult(generations=[generation], llm_output=llm_output)

        except Exception as e:
            logger.error(f"思考模型适配器异步生成失败: {e}")
            # 返回错误消息
            error_message = AIMessage(content=f"思考模型生成失败: {str(e)}")
            generation = ChatGeneration(message=error_message)
            return ChatResult(generations=[generation])

    async def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ):
        """
        流式生成聊天响应（支持思考内容）

        Args:
            messages: 消息列表
            stop: 停止词列表
            run_manager: 回调管理器
            **kwargs: 额外参数

        Yields:
            ChatGenerationChunk: 包含内容或思考内容的块
        """
        from langchain_core.messages import AIMessageChunk
        from langchain_core.outputs import ChatGenerationChunk

        try:
            # 转换消息格式
            formatted_messages = self._format_messages(messages)

            # 添加停止词
            if stop:
                kwargs["stop"] = stop

            # 收集思考内容
            reasoning_buffer = []

            # 调用思考模型客户端的流式方法（包含思考内容）
            async for (
                content,
                reasoning,
            ) in self._reasoning_client._stream_with_reasoning_internal(
                formatted_messages, **kwargs
            ):
                # 收集思考内容
                if reasoning:
                    reasoning_buffer.append(reasoning)

                # 构建消息块
                additional_kwargs = {}
                if reasoning:
                    additional_kwargs["reasoning_content"] = reasoning

                message_chunk = AIMessageChunk(
                    content=content,
                    additional_kwargs=additional_kwargs,
                )

                yield ChatGenerationChunk(message=message_chunk)

            # 流式结束，完整思考内容已在 reasoning_buffer 中
            if reasoning_buffer:
                logger.debug(
                    f"[ReasoningLangChainAdapter] 流式完成 | 思考内容长度: {len(''.join(reasoning_buffer))}"
                )

        except Exception as e:
            logger.error(f"思考模型适配器流式生成失败: {e}")
            # 返回错误消息块
            error_message = AIMessageChunk(content=f"思考模型流式生成失败: {str(e)}")
            yield ChatGenerationChunk(message=error_message)

    async def astream_with_thinking(
        self,
        messages: list[BaseMessage],
        **kwargs: Any,
    ) -> AsyncIterator:
        """
        流式生成（兼容系统接口）

        系统直接调用此方法进行流式输出，而不是使用 LangChain 的标准 astream

        Args:
            messages: 消息列表
            **kwargs: 额外参数

        Yields:
            AIMessageChunk: 包含内容和思考内容的块
        """
        from langchain_core.messages import AIMessageChunk

        try:
            # 转换消息格式
            formatted_messages = self._format_messages(messages)

            # 收集思考内容
            reasoning_buffer = []

            # 调用思考模型客户端的流式方法（包含思考内容）
            async for (
                content,
                reasoning,
            ) in self._reasoning_client._stream_with_reasoning_internal(
                formatted_messages, **kwargs
            ):
                # 收集思考内容
                if reasoning:
                    reasoning_buffer.append(reasoning)

                # 构建消息块
                additional_kwargs = {}
                if reasoning:
                    additional_kwargs["reasoning_content"] = reasoning

                message_chunk = AIMessageChunk(
                    content=content,
                    additional_kwargs=additional_kwargs,
                )

                yield message_chunk

            # 流式结束
            if reasoning_buffer:
                logger.debug(
                    f"[ReasoningLangChainAdapter] astream_with_thinking 完成 | 思考内容长度: {len(''.join(reasoning_buffer))}"
                )

        except Exception as e:
            logger.error(f"思考模型适配器 astream_with_thinking 失败: {e}")
            # 返回错误消息块
            error_message = AIMessageChunk(content=f"思考模型流式生成失败: {str(e)}")
            yield error_message

    def _format_messages(self, messages: list[BaseMessage]) -> list:
        """
        格式化消息为 API 格式

        Args:
            messages: LangChain 消息列表

        Returns:
            Message 对象列表
        """
        from src.llm.base import Message

        formatted = []

        for message in messages:
            if isinstance(message, HumanMessage):
                formatted.append(Message(role="user", content=message.content))
            elif isinstance(message, AIMessage):
                formatted.append(Message(role="assistant", content=message.content))
            elif isinstance(message, SystemMessage):
                formatted.append(Message(role="system", content=message.content))
            else:
                # 其他类型消息转为用户消息
                formatted.append(Message(role="user", content=str(message.content)))

        return formatted

    async def ainvoke(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """
        异步调用聊天模型（单次）

        Args:
            messages: 消息列表
            stop: 停止词列表
            run_manager: 回调管理器
            **kwargs: 额外参数

        Returns:
            AIMessage: AI 消息
        """
        result = await self._agenerate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        return result.generations[0].message

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """返回识别参数"""
        return {
            "model_name": self._reasoning_client.model_name,
            "reasoning_type": self._reasoning_client.reasoning_type,
        }

    def _convert_tools(self, tools: list[Any]) -> list[dict[str, Any]]:
        """
        转换工具为 OpenAI 格式

        Args:
            tools: LangChain 工具列表

        Returns:
            OpenAI 格式的工具列表
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.args_schema.schema()
                    if tool.args_schema
                    else {"type": "object", "properties": {}, "required": []},
                },
            }
            for tool in tools
        ]

    def bind_tools(
        self,
        tools: list[Any],
        **kwargs: Any,
    ) -> "ReasoningLangChainAdapter":
        """
        绑定工具到模型

        注意：某些思考模型（如 DeepSeek R1）支持工具调用，但需要特殊处理

        Args:
            tools: LangChain 工具列表
            **kwargs: 额外参数

        Returns:
            自身（工具绑定存储在实例属性中）

        Raises:
            TypeError: 如果模型不支持工具调用
        """
        # 检查底层思考模型是否支持工具调用
        try:
            from src.llm.reasoning_config import ReasoningConfig

            model_name = getattr(self._reasoning_client, "model_name", None)
            if model_name and ReasoningConfig.supports_tools(model_name):
                # 模型支持工具调用，存储工具列表
                self._bound_tools = tools
                tool_names = []
                for tool in tools:
                    if hasattr(tool, "name"):
                        tool_names.append(tool.name)
                    elif isinstance(tool, type):
                        tool_names.append(tool.__name__)
                    else:
                        tool_names.append(str(tool))

                logger.info(
                    f"[ReasoningLangChainAdapter] 工具绑定成功 | "
                    f"model={model_name} | 工具列表={tool_names}"
                )
                return self
        except Exception as e:
            logger.warning(f"[ReasoningLangChainAdapter] 检查工具支持失败: {e}")

        # 模型不支持工具调用
        tool_names = []
        for tool in tools:
            if hasattr(tool, "name"):
                tool_names.append(tool.name)
            elif isinstance(tool, type):
                tool_names.append(tool.__name__)
            else:
                tool_names.append(str(tool))

        # 检查是否强制要求工具调用
        force_tools = kwargs.get("force_tools", False)
        if force_tools:
            raise TypeError(
                f"[ReasoningLangChainAdapter] 思考模型不支持工具调用，"
                f"但配置要求强制使用工具 | "
                f"工具列表: {tool_names} | "
                f"请使用普通模型或禁用工具调用"
            )

        logger.warning(
            f"[ReasoningLangChainAdapter] 思考模型暂不支持工具调用 | "
            f"工具列表: {tool_names} | "
            f"将忽略工具绑定，使用普通生成模式 | "
            f"建议: 在有工具的场景下使用普通模型"
        )

        # 返回自身，不实际绑定工具
        return self
