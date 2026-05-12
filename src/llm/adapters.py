"""
LLM 适配器

将自研 LLM 客户端适配为 LangChain BaseChatModel
"""

import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult

from src.llm.base import LLMClient, Message, Tool


class LLMClientAdapter(BaseChatModel):
    """
    自研 LLM 客户端的 LangChain 适配器

    将 src.llm.base.LLMClient 适配为 LangChain BaseChatModel
    """

    client: LLMClient
    """底层 LLM 客户端"""

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, client: LLMClient, **kwargs):
        """
        初始化适配器

        Args:
            client: 自研 LLM 客户端
        """
        super().__init__(client=client, **kwargs)

    @property
    def _llm_type(self) -> str:
        """返回 LLM 类型标识"""
        return f"custom_{self.client.model_name}"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        """返回标识参数"""
        return {
            "model_name": self.client.model_name,
            "api_base": self.client.api_base,
        }

    def _convert_messages_to_native(self, messages: list[BaseMessage]) -> list[Message]:
        """
        将 LangChain 消息转换为自研格式

        Args:
            messages: LangChain 消息列表

        Returns:
            自研格式消息列表
        """
        native_messages = []

        for msg in messages:
            if isinstance(msg, SystemMessage):
                native_messages.append(Message(role="system", content=msg.content))
            elif isinstance(msg, HumanMessage):
                native_messages.append(Message(role="user", content=msg.content))
            elif isinstance(msg, AIMessage):
                native_msg = Message(role="assistant", content=msg.content)
                # 处理工具调用
                if msg.tool_calls:
                    from src.llm.base import ToolCall

                    native_msg.tool_calls = [
                        ToolCall(
                            id=tc.get("id", ""),
                            name=tc.get("name", ""),
                            arguments=tc.get("args", {}),
                        )
                        for tc in msg.tool_calls
                    ]
                native_messages.append(native_msg)
            elif isinstance(msg, ToolMessage):
                native_messages.append(
                    Message(
                        role="tool",
                        content=msg.content,
                        tool_call_id=msg.tool_call_id,
                        name=msg.name,
                    )
                )

        return native_messages

    def _convert_response_to_langchain(self, response: Any) -> AIMessage:
        """
        将自研响应转换为 LangChain AIMessage

        Args:
            response: 自研 LLM 响应

        Returns:
            LangChain AIMessage
        """
        content = response.content or ""
        tool_calls = []

        if response.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "name": tc.name,
                    "args": tc.arguments,
                }
                for tc in response.tool_calls
            ]

        return AIMessage(
            content=content,
            tool_calls=tool_calls,
            additional_kwargs={
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }
            },
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        同步生成（不支持，抛出异常）
        """
        raise NotImplementedError("LLMClientAdapter 不支持同步调用，请使用 ainvoke")

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        """
        异步生成

        Args:
            messages: 消息列表
            stop: 停止词
            run_manager: 回调管理器
            **kwargs: 额外参数

        Returns:
            ChatResult
        """
        # 转换消息
        native_messages = self._convert_messages_to_native(messages)

        # 检查是否有工具
        tools = kwargs.get("tools", [])

        if tools:
            import logging

            logger = logging.getLogger(__name__)

            # 转换工具格式
            native_tools = []
            for idx, t in enumerate(tools):
                # 获取工具参数 schema
                parameters = {}

                # 方法1: 从 LangChain StructuredTool 提取 args_schema
                if hasattr(t, "args_schema") and t.args_schema:
                    if hasattr(t.args_schema, "schema") and callable(
                        getattr(t.args_schema, "schema", None)
                    ):
                        # Pydantic 模型，调用 schema() 方法
                        parameters = t.args_schema.schema()
                        logger.debug(
                            f"[LLMAdapter] 工具 {idx}: {t.name} | "
                            f"从 args_schema.schema() 获取参数"
                        )
                    elif isinstance(t.args_schema, dict):
                        # 已经是字典，直接使用
                        parameters = t.args_schema
                        logger.debug(
                            f"[LLMAdapter] 工具 {idx}: {t.name} | "
                            f"args_schema 是字典，直接使用"
                        )
                    else:
                        # 其他类型，尝试转换为字典
                        try:
                            parameters = dict(t.args_schema)
                            logger.debug(
                                f"[LLMAdapter] 工具 {idx}: {t.name} | "
                                f"args_schema 转换为字典"
                            )
                        except (TypeError, ValueError) as e:
                            # 转换失败，使用空字典
                            logger.warning(
                                f"[LLMAdapter] 工具 {idx}: {t.name} | "
                                f"args_schema 转换失败: {e}，使用空schema"
                            )
                            parameters = {}

                # 方法2: 检查是否有 _fields 属性（Pydantic 模型）
                elif (
                    hasattr(t.args_schema, "_fields")
                    if hasattr(t, "args_schema")
                    else False
                ):
                    try:
                        parameters = t.args_schema.schema()
                        logger.debug(
                            f"[LLMAdapter] 工具 {idx}: {t.name} | 从 _fields 获取参数"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[LLMAdapter] 工具 {idx}: {t.name} | _fields 转换失败: {e}"
                        )
                        parameters = {}

                # 验证参数 schema 格式
                if not isinstance(parameters, dict):
                    logger.warning(
                        f"[LLMAdapter] 工具 {idx}: {t.name} | "
                        f"parameters 不是字典，使用空schema"
                    )
                    parameters = {}

                if "type" not in parameters:
                    parameters["type"] = "object"
                if "properties" not in parameters:
                    parameters["properties"] = {}
                if "required" not in parameters:
                    parameters["required"] = []

                logger.info(
                    f"[LLMAdapter] 工具转换 | "
                    f"name={t.name} | "
                    f"description={t.description[:50]}... | "
                    f"parameters_keys={list(parameters.get('properties', {}).keys())}"
                )

                native_tools.append(
                    Tool(
                        name=t.name,
                        description=t.description,
                        parameters=parameters,
                    )
                )

            logger.info(
                f"[LLMAdapter] 工具转换完成 | "
                f"总数={len(tools)} | "
                f"成功={len(native_tools)}"
            )

            # 从 kwargs 中移除 tools 参数，避免重复传递
            filtered_kwargs = {k: v for k, v in kwargs.items() if k != "tools"}
            response = await self.client.generate_with_tools(
                messages=native_messages,
                tools=native_tools,
                **filtered_kwargs,
            )
        else:
            response = await self.client.generate(
                messages=native_messages,
                **kwargs,
            )

        # 转换响应
        ai_message = self._convert_response_to_langchain(response)

        return ChatResult(
            generations=[ChatGeneration(message=ai_message)],
            llm_output={
                "model": response.model,
                "usage": {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                },
            },
        )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AIMessageChunk]:
        """
        流式生成（避免产生大量空事件）

        Args:
            messages: 消息列表
            stop: 停止词列表
            run_manager: 回调管理器
            **kwargs: 额外参数

        Yields:
            AIMessageChunk: 消息块（只 yield 有内容的块）
        """
        # 转换消息
        native_messages = self._convert_messages_to_native(messages)

        # 检查是否有工具
        tools = kwargs.get("tools", [])

        # 调用客户端的流式方法
        if tools:
            # 有工具时，使用 stream_with_tools
            async for content in self.client.stream_with_tools(
                native_messages, tools, **kwargs
            ):
                if content:  # 只 yield 有内容的块
                    yield AIMessageChunk(content=content)
        else:
            # 无工具时，使用 stream
            async for content in self.client.stream(native_messages, **kwargs):
                if content:  # 只 yield 有内容的块
                    yield AIMessageChunk(content=content)

    def bind_tools(self, tools: list[Any]) -> "LLMClientAdapter":
        """
        绑定工具到 LLM 客户端

        Args:
            tools: LangChain 工具列表

        Returns:
            绑定工具后的适配器实例
        """
        logger = logging.getLogger(__name__)

        logger.info(f"[LLMClientAdapter.bind_tools] 开始绑定工具 | 工具数={len(tools)}")

        # 创建新实例并存储工具
        new_adapter = LLMClientAdapter(self.client)
        new_adapter._bound_tools = tools

        # 验证工具格式
        valid_tools = []
        for idx, tool in enumerate(tools):
            tool_name = getattr(tool, "name", f"unknown_{idx}")
            tool_desc = getattr(tool, "description", "")

            # 检查必需属性
            if not hasattr(tool, "name") or not tool.name:
                logger.warning(f"[bind_tools] 工具 {idx} 缺少 name 属性，跳过")
                continue

            if not hasattr(tool, "args_schema"):
                logger.warning(f"[bind_tools] 工具 {tool_name} 缺少 args_schema")

            valid_tools.append(tool)
            logger.debug(
                f"[bind_tools] 工具 {idx}: {tool_name} | "
                f"description={tool_desc[:30] if tool_desc else '无'}... | "
                f"has_schema={hasattr(tool, 'args_schema')}"
            )

        logger.info(
            f"[LLMClientAdapter.bind_tools] 工具绑定完成 | "
            f"输入={len(tools)} | "
            f"有效={len(valid_tools)} | "
            f"工具列表={[t.name for t in valid_tools]}"
        )

        new_adapter._bound_tools = valid_tools
        return new_adapter

    async def ainvoke(
        self,
        input: Any,
        config: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AIMessage:
        """
        异步调用

        Args:
            input: 输入（消息列表或字符串）
            config: 配置（包含思考模式等参数）
            **kwargs: 额外参数

        Returns:
            AIMessage
        """
        logger = logging.getLogger(__name__)

        # 处理输入
        if isinstance(input, str):
            messages = [HumanMessage(content=input)]
        elif isinstance(input, list):
            messages = input
        else:
            messages = [input]

        # 添加绑定的工具（关键）
        # 重要：将 LangChain StructuredTool 转换为内部 Tool 对象
        if hasattr(self, "_bound_tools") and self._bound_tools:
            tools_count = len(self._bound_tools)
            tool_names = [getattr(t, "name", str(t)) for t in self._bound_tools]
            logger.info(
                f"[LLMClientAdapter.ainvoke] 添加绑定工具 | "
                f"工具数={tools_count} | "
                f"工具列表={tool_names}"
            )

            # 关键修复：检查工具类型并转换
            from src.llm.base import Tool as InternalTool

            converted_tools = []
            for idx, tool in enumerate(self._bound_tools):
                tool_name = getattr(tool, "name", f"unknown_{idx}")

                # 检查是否已经是内部 Tool 类型
                if isinstance(tool, InternalTool):
                    logger.debug(
                        f"[LLMClientAdapter.ainvoke] 工具 {idx}: {tool_name} | "
                        f"类型=InternalTool，直接使用"
                    )
                    converted_tools.append(tool)
                else:
                    # LangChain StructuredTool，需要转换
                    logger.debug(
                        f"[LLMClientAdapter.ainvoke] 工具 {idx}: {tool_name} | "
                        f"类型={type(tool).__name__}，需要转换"
                    )

                    # 提取参数 schema
                    parameters = {}
                    if hasattr(tool, "args_schema") and tool.args_schema:
                        try:
                            if hasattr(tool.args_schema, "schema"):
                                # Pydantic 模型
                                parameters = tool.args_schema.schema()
                            elif isinstance(tool.args_schema, dict):
                                parameters = tool.args_schema
                            else:
                                parameters = dict(tool.args_schema)
                        except Exception as e:
                            logger.warning(
                                f"[LLMClientAdapter.ainvoke] 工具 {tool_name} 参数转换失败: {e}"
                            )
                            parameters = {}

                    # 确保 JSON Schema 必需字段
                    if "type" not in parameters:
                        parameters["type"] = "object"
                    if "properties" not in parameters:
                        parameters["properties"] = {}
                    if "required" not in parameters:
                        parameters["required"] = []

                    logger.info(
                        f"[LLMClientAdapter.ainvoke] 工具转换 | "
                        f"name={tool_name} | "
                        f"parameters_keys={list(parameters.get('properties', {}).keys())}"
                    )

                    converted_tools.append(
                        InternalTool(
                            name=tool.name,
                            description=tool.description,
                            parameters=parameters,
                        )
                    )

            kwargs["tools"] = converted_tools
            logger.info(
                f"[LLMClientAdapter.ainvoke] 工具转换完成 | "
                f"输入={len(self._bound_tools)} | "
                f"输出={len(converted_tools)}"
            )
        else:
            logger.debug(
                f"[LLMClientAdapter.ainvoke] 无绑定工具 | "
                f"has_bound_tools={hasattr(self, '_bound_tools')}"
            )

        # 将 config 中的参数合并到 kwargs（用于思考模式等）
        if config:
            kwargs.update(config)
            logger.debug(
                f"[LLMClientAdapter.ainvoke] 合并 config 参数 | "
                f"config_keys={list(config.keys())}"
            )

        # 优先使用基类的 ainvoke 方法（自带日志记录）
        # 只有在基类方法不存在时才使用子类实现
        # 这样可以确保所有客户端都有日志记录
        try:
            # 尝试使用基类的 ainvoke（通过 super() 调用）
            # 这样会自动调用 generate/generate_with_tools，确保日志记录
            result = await LLMClient.ainvoke(self.client, messages, config, **kwargs)

            # 记录结果中的工具调用
            if hasattr(result, "tool_calls") and result.tool_calls:
                tool_names = [tc.get("name", "") for tc in result.tool_calls]
                logger.info(
                    f"[LLMClientAdapter.ainvoke] 返回工具调用 | "
                    f"count={len(result.tool_calls)} | "
                    f"tools={tool_names}"
                )
            else:
                logger.debug("[LLMClientAdapter.ainvoke] 未返回工具调用")

            return result
        except Exception as e:
            logger.debug(f"[LLMClientAdapter] 基类 ainvoke 失败，使用适配器方法: {e}")
            # 降级到适配器的 _agenerate 方法
            result = await self._agenerate(messages, **kwargs)
            return result.generations[0].message
