"""
智谱 AI 客户端

专门处理智谱 API 的思考模式（reasoning_content）
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage, AIMessageChunk

from src.core.exceptions import (
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
)
from src.core.exceptions import (
    LLMException as LLMError,
)
from src.llm.base import (
    LLMClient,
    LLMResponse,
    Message,
    TokenUsage,
    Tool,
    ToolCall,
    messages_to_langchain,
)

logger = logging.getLogger(__name__)


class ZhipuClient(LLMClient):
    """
    智谱 AI 客户端

    支持 GLM 系列模型，特别处理思考模式的 reasoning_content 字段
    """

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        api_base: str | None = None,
        default_params: dict[str, Any] | None = None,
        enable_concurrency_control: bool = True,
    ):
        """
        初始化智谱客户端

        Args:
            model_name: 模型名称（如 glm-4.7）
            api_key: API 密钥
            api_base: API 基础 URL
            default_params: 默认参数
            enable_concurrency_control: 是否启用并发控制
        """
        super().__init__(
            model_name,
            api_key,
            api_base or "https://open.bigmodel.cn/api/paas/v4",
            default_params,
            provider="zhipu",
            enable_concurrency_control=enable_concurrency_control,
        )

        # 初始化智谱 SDK 客户端
        try:
            from zhipuai import ZhipuAI

            self._client = ZhipuAI(api_key=api_key)
        except ImportError:
            logger.warning("zhipuai SDK 未安装，将使用 OpenAI 兼容模式")
            self._client = None

        # 同时保留 OpenAI 兼容客户端作为备选
        from langchain_openai import ChatOpenAI

        self._openai_client = ChatOpenAI(
            model=model_name,
            api_key=api_key,
            base_url=api_base or "https://open.bigmodel.cn/api/paas/v4",
            timeout=60.0,  # 设置60秒超时
            request_timeout=60.0,  # 请求超时
        )

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """转换消息格式为智谱 API 格式"""
        result = []
        for msg in messages:
            item = {"role": msg.role, "content": msg.content or ""}
            if msg.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            if msg.name:
                item["name"] = msg.name
            result.append(item)
        return result

    def _convert_tools(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """
        转换工具格式为 OpenAI 兼容格式

        Args:
            tools: 工具列表（内部 Tool 对象）

        Returns:
            OpenAI 格式的工具定义列表
        """
        logger.info(
            f"[_convert_tools] 开始转换 | "
            f"输入工具数={len(tools)} | "
            f"工具类型={type(tools[0]).__name__ if tools else 'None'}"
        )

        converted_tools = []
        for idx, tool in enumerate(tools):
            # 验证工具定义
            tool_name = getattr(tool, "name", None)
            if not tool_name:
                logger.warning(f"[_convert_tools] 工具 {idx} 缺少 name，跳过")
                continue

            # 确保 parameters 字段存在且格式正确
            parameters = (
                getattr(tool, "parameters", {}) if hasattr(tool, "parameters") else {}
            )

            if not isinstance(parameters, dict):
                logger.warning(
                    f"[_convert_tools] 工具 {tool_name} 的 parameters 不是字典，使用空schema"
                )
                parameters = {}

            # 确保 JSON Schema 必需字段存在
            if "type" not in parameters:
                parameters["type"] = "object"
            if "properties" not in parameters:
                parameters["properties"] = {}
            if "required" not in parameters:
                parameters["required"] = []

            # 验证参数完整性
            param_keys = list(parameters.get("properties", {}).keys())
            logger.debug(
                f"[_convert_tools] 工具 {idx}: {tool_name} | "
                f"description={tool.description[:50] if hasattr(tool, 'description') and tool.description else '无'}... | "
                f"parameters_keys={param_keys}"
            )

            # 记录工具详情
            logger.info(
                f"[_convert_tools] 工具转换成功 | "
                f"name={tool_name} | "
                f"parameters_count={len(param_keys)} | "
                f"required={parameters.get('required', [])}"
            )

            converted_tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": getattr(tool, "description", ""),
                        "parameters": parameters,
                    },
                }
            )

        logger.info(
            f"[_convert_tools] 工具转换完成 | "
            f"输入={len(tools)} | "
            f"成功={len(converted_tools)}"
        )

        # 输出最终的工具定义（用于调试）
        if converted_tools:
            logger.debug(
                f"[_convert_tools] 最终工具定义 | "
                f"tools={[t['function']['name'] for t in converted_tools]}"
            )

        return converted_tools

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
        # 使用 OpenAI 兼容客户端
        lc_messages = messages_to_langchain(messages)
        response = await self._openai_client.ainvoke(lc_messages)

        return LLMResponse(
            content=response.content if isinstance(response.content, str) else "",
            usage=TokenUsage(),
            model=self.model_name,
        )

    async def _stream_internal(
        self,
        messages: list[Message],
        **kwargs,
    ) -> AsyncIterator[str]:
        """内部流式生成实现"""
        lc_messages = messages_to_langchain(messages)
        async for chunk in self._openai_client.astream(lc_messages):
            if chunk.content:
                yield chunk.content

    async def _generate_with_tools_internal(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs,
    ) -> LLMResponse:
        """
        内部带工具调用生成实现

        Args:
            messages: 消息列表
            tools: 工具列表
            **kwargs: 额外参数

        Returns:
            LLM 响应
        """
        logger.info(
            f"[_generate_with_tools_internal] 开始 | "
            f"消息数={len(messages)} | "
            f"工具数={len(tools)}"
        )

        # 转换消息
        lc_messages = messages_to_langchain(messages)

        # 转换工具
        openai_tools = self._convert_tools(tools)

        # 记录即将发送的工具定义
        if openai_tools:
            logger.info(
                f"[_generate_with_tools_internal] 工具定义 | "
                f"count={len(openai_tools)} | "
                f"tools={[t['function']['name'] for t in openai_tools]}"
            )
            # 验证每个工具的 parameters 字段
            for tool_def in openai_tools:
                func_def = tool_def.get("function", {})
                params = func_def.get("parameters", {})
                if not params:
                    logger.warning(f"工具 {func_def.get('name')} 的 parameters 为空！")
        else:
            logger.warning("[_generate_with_tools_internal] 没有可用的工具定义")

        # 绑定工具并调用
        model_with_tools = self._openai_client.bind(tools=openai_tools)
        response = await model_with_tools.ainvoke(lc_messages)

        # 处理工具调用
        tool_calls = None
        if response.tool_calls:
            logger.info(
                f"[_generate_with_tools_internal] LLM返回工具调用 | "
                f"count={len(response.tool_calls)}"
            )
            tool_calls = [
                ToolCall(
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("args", {}),
                )
                for tc in response.tool_calls
            ]
        else:
            logger.debug("[_generate_with_tools_internal] LLM未返回工具调用")

        return LLMResponse(
            content=response.content if isinstance(response.content, str) else "",
            tool_calls=tool_calls,
            usage=TokenUsage(),
            model=self.model_name,
        )

    async def ainvoke(
        self,
        messages,
        config: dict[str, Any] | None = None,
        **kwargs,
    ) -> AIMessage:
        """
        异步调用（LangChain 兼容接口）

        支持思考模式，将 reasoning_content 注入到 additional_kwargs

        Args:
            messages: 消息列表
            config: 配置参数（LangChain 风格）
            **kwargs: 额外参数（包括 thinking 等）

        Returns:
            AIMessage，思考内容在 additional_kwargs["reasoning_content"] 中
        """
        # 合并 config 和 kwargs
        if config:
            kwargs.update(config)

        params = self._merge_params(**kwargs)
        thinking_enabled = "thinking" in params

        # 如果启用思考模式且有原生客户端，使用原生 SDK
        if thinking_enabled and self._client:
            return await self._ainvoke_with_thinking(messages, params)

        # 否则使用 OpenAI 兼容客户端
        return await self._ainvoke_openai_compat(messages, params)

    async def _ainvoke_with_thinking(
        self,
        messages,
        params: dict[str, Any],
    ) -> AIMessage:
        """使用智谱原生 SDK 调用，支持思考模式"""
        import asyncio

        # 转换消息格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                # LangChain 格式，转换为字典
                api_messages = []
                for msg in messages:
                    api_messages.append(
                        {
                            "role": getattr(msg, "type", "user"),
                            "content": msg.content,
                        }
                    )
                    # 修正 role
                    if api_messages[-1]["role"] == "human":
                        api_messages[-1]["role"] = "user"
                    elif api_messages[-1]["role"] == "ai":
                        api_messages[-1]["role"] = "assistant"
            else:
                api_messages = self._convert_messages(messages)
        else:
            api_messages = self._convert_messages(messages)

        # 构建请求参数
        request_params = {
            "model": self.model_name,
            "messages": api_messages,
        }

        # 添加思考模式参数
        if "thinking" in params:
            request_params["thinking"] = params["thinking"]

        # ===== 关键修复: 添加工具处理逻辑 =====
        # 检查是否有工具参数
        tools = params.get("tools")
        if tools:
            logger.info(
                f"[_ainvoke_with_thinking] 检测到工具参数 | 工具数={len(tools)}"
            )
            # 转换工具格式
            openai_tools = self._convert_tools(tools)
            if openai_tools:
                request_params["tools"] = openai_tools
                tool_names = [t["function"]["name"] for t in openai_tools]
                logger.info(
                    f"[_ainvoke_with_thinking] 工具已添加到请求 | "
                    f"count={len(openai_tools)} | "
                    f"tools={tool_names}"
                )
            else:
                logger.error("[_ainvoke_with_thinking] 工具转换失败!")
        # ===== 修复结束 =====

        # 添加其他参数
        for key in ["temperature", "max_tokens"]:
            if key in params:
                request_params[key] = params[key]

        logger.info(
            f"[智谱 API] 调用 | model={self.model_name} | "
            f"thinking={bool(params.get('thinking'))}"
        )

        try:
            # 在线程池中执行同步调用
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, lambda: self._client.chat.completions.create(**request_params)
            )

            # 提取响应内容
            choice = response.choices[0]
            message = choice.message

            content = message.content or ""

            # 提取思考内容（多种方式尝试）
            reasoning_content = None

            # 方式1: 直接属性 reasoning_content
            reasoning_content = getattr(message, "reasoning_content", None)

            # 方式2: 检查响应对象的其他属性
            if not reasoning_content and hasattr(message, "__dict__"):
                for key, value in message.__dict__.items():
                    if "reasoning" in key.lower() or "thinking" in key.lower():
                        if value and isinstance(value, str):
                            reasoning_content = value
                            logger.info(f"[智谱 API] 从 {key} 提取思考内容")
                            break

            if reasoning_content:
                logger.info(
                    f"[智谱 API] 思考内容提取成功 | len={len(reasoning_content)}"
                )

            # 构建 additional_kwargs
            additional_kwargs = {}
            if reasoning_content:
                additional_kwargs["reasoning_content"] = reasoning_content
                logger.info(
                    f"[智谱 API] 思考内容已添加到 additional_kwargs | len={len(reasoning_content)}"
                )
            else:
                logger.warning(
                    "[智谱 API] 响应中未找到 reasoning_content，思考模式可能未启用"
                )

            # 处理工具调用
            tool_calls = []
            if hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "args": json.loads(tc.function.arguments),
                        }
                    )

            # 构建 response_metadata
            response_metadata = {
                "model_name": self.model_name,
                "finish_reason": choice.finish_reason,
            }
            if hasattr(response, "usage") and response.usage:
                response_metadata["token_usage"] = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return AIMessage(
                content=content,
                additional_kwargs=additional_kwargs,
                tool_calls=tool_calls,
                response_metadata=response_metadata,
            )

        except Exception as e:
            logger.error(f"[智谱 API] 错误 | error={str(e)}")
            self._handle_error(e)

    async def _ainvoke_openai_compat(
        self,
        messages,
        params: dict[str, Any],
    ) -> AIMessage:
        """使用 OpenAI 兼容客户端调用"""
        # 检查是否已经是 LangChain 格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                lc_messages = messages
            else:
                lc_messages = messages_to_langchain(messages)
        else:
            lc_messages = messages_to_langchain(messages)

        # 构建绑定参数
        bind_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                bind_params[key] = params[key]

        # 思考模式和结构化输出通过 extra_body 传递
        extra_body = {}
        if "thinking" in params:
            extra_body["thinking"] = params["thinking"]

        # 结构化输出参数
        if "response_format" in params:
            response_format = params["response_format"]
            if response_format and isinstance(response_format, dict):
                rf_type = response_format.get("type")
                if rf_type in ("json_schema", "json_object"):
                    extra_body["response_format"] = response_format
                    logger.info(f"[智谱 API] 启用结构化输出 | type={rf_type}")

        if extra_body:
            bind_params["extra_body"] = extra_body

        # 处理工具绑定（关键修复）
        # 检查是否有工具参数（通过 LLMClientAdapter 传递）
        tools = params.get("tools")
        if tools:
            logger.info(
                f"[_ainvoke_openai_compat] 检测到工具参数 | "
                f"工具数={len(tools)} | "
                f"第一个工具类型={type(tools[0]).__name__ if tools else 'None'}"
            )
            # 转换工具格式
            openai_tools = self._convert_tools(tools)
            if openai_tools:
                bind_params["tools"] = openai_tools
                tool_names = [t["function"]["name"] for t in openai_tools]
                logger.info(
                    f"[_ainvoke_openai_compat] 工具已绑定 | "
                    f"count={len(openai_tools)} | "
                    f"tools={tool_names}"
                )
            else:
                logger.error(
                    "[_ainvoke_openai_compat] 工具转换失败，openai_tools 为空！"
                )
        else:
            logger.debug("[_ainvoke_openai_compat] 未检测到工具参数")

        if bind_params:
            model = self._openai_client.bind(**bind_params)
            logger.debug(
                f"[_ainvoke_openai_compat] 模型绑定完成 | "
                f"params={list(bind_params.keys())}"
            )
        else:
            model = self._openai_client

        response = await model.ainvoke(lc_messages)

        # 记录响应中的工具调用
        if hasattr(response, "tool_calls") and response.tool_calls:
            tool_names = [tc.get("name", "") for tc in response.tool_calls]
            logger.info(
                f"[_ainvoke_openai_compat] LLM返回工具调用 | "
                f"count={len(response.tool_calls)} | "
                f"tools={tool_names}"
            )
        else:
            logger.debug("[_ainvoke_openai_compat] LLM未返回工具调用")

        return response

    def bind_tools(self, tools: list[Tool]):
        """绑定工具"""
        return _BoundZhipuClient(self, tools)

    async def astream_with_thinking(
        self,
        messages,
        **kwargs,
    ) -> AsyncIterator[AIMessageChunk]:
        """
        流式调用，支持思考模式

        Yields:
            AIMessageChunk，思考内容在 additional_kwargs["reasoning_content"] 中
        """
        params = self._merge_params(**kwargs)

        if not self._client:
            # 无原生客户端，使用 OpenAI 兼容模式
            async for chunk in self._openai_client.astream(messages):
                yield chunk
            return

        import asyncio

        # 转换消息格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                api_messages = []
                for msg in messages:
                    api_messages.append(
                        {
                            "role": getattr(msg, "type", "user"),
                            "content": msg.content,
                        }
                    )
                    if api_messages[-1]["role"] == "human":
                        api_messages[-1]["role"] = "user"
                    elif api_messages[-1]["role"] == "ai":
                        api_messages[-1]["role"] = "assistant"
            else:
                api_messages = self._convert_messages(messages)
        else:
            api_messages = self._convert_messages(messages)

        # 构建请求参数
        request_params = {
            "model": self.model_name,
            "messages": api_messages,
            "stream": True,
        }

        if "thinking" in params:
            request_params["thinking"] = params["thinking"]

        for key in ["temperature", "max_tokens"]:
            if key in params:
                request_params[key] = params[key]

        logger.info(
            f"[智谱 API 流式] 调用 | model={self.model_name} | "
            f"thinking={bool(params.get('thinking'))}"
        )

        try:
            # 在线程池中执行同步流式调用
            loop = asyncio.get_event_loop()

            def stream_generator():
                return self._client.chat.completions.create(**request_params)

            response = await loop.run_in_executor(None, stream_generator)

            # 迭代流式响应
            for chunk in response:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None) or ""
                reasoning = getattr(delta, "reasoning_content", None) or ""

                additional_kwargs = {}
                if reasoning:
                    additional_kwargs["reasoning_content"] = reasoning

                yield AIMessageChunk(
                    content=content,
                    additional_kwargs=additional_kwargs,
                )

        except Exception as e:
            logger.error(f"[智谱 API 流式] 错误 | error={str(e)}")
            self._handle_error(e)


class _BoundZhipuClient:
    """绑定工具后的智谱客户端包装器"""

    def __init__(self, client: ZhipuClient, tools: list[Tool]):
        self._client = client
        self._tools = tools

    async def ainvoke(self, messages, config: dict[str, Any] | None = None, **kwargs):
        """
        调用绑定工具的模型

        支持思考模式 + 工具调用同时使用
        """
        # 合并 config 和 kwargs
        if config:
            kwargs.update(config)

        params = self._client._merge_params(**kwargs)
        thinking_enabled = "thinking" in params

        # 如果启用思考模式且有原生客户端，使用原生 SDK（支持思考+工具）
        if thinking_enabled and self._client._client:
            return await self._ainvoke_with_thinking_and_tools(messages, params)

        # 否则使用 OpenAI 兼容模式
        openai_tools = self._client._convert_tools(self._tools)
        model = self._client._openai_client.bind(tools=openai_tools)

        # 检查是否已经是 LangChain 格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                lc_messages = messages
            else:
                lc_messages = messages_to_langchain(messages)
        else:
            lc_messages = messages_to_langchain(messages)

        return await model.ainvoke(lc_messages, **kwargs)

    async def _ainvoke_with_thinking_and_tools(
        self,
        messages,
        params: dict[str, Any],
    ) -> AIMessage:
        """使用智谱原生 SDK 调用，支持思考模式 + 工具调用"""
        import asyncio

        # 转换消息格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                api_messages = []
                for msg in messages:
                    msg_dict = {
                        "role": getattr(msg, "type", "user"),
                        "content": msg.content,
                    }
                    # 修正 role
                    if msg_dict["role"] == "human":
                        msg_dict["role"] = "user"
                    elif msg_dict["role"] == "ai":
                        msg_dict["role"] = "assistant"
                    # 处理工具调用消息
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        tool_calls_list = []
                        for tc in msg.tool_calls:
                            # 处理 ToolCall 对象或字典格式
                            if hasattr(tc, "id"):
                                tc_id = tc.id
                                tc_name = tc.name
                                tc_args = tc.arguments if hasattr(tc, "arguments") else {}
                            else:
                                tc_id = tc.get("id", "")
                                tc_name = tc.get("name", "")
                                tc_args = tc.get("args", tc.get("arguments", {}))

                            # 创建 ToolCall 对象并添加到 content_blocks
                            tool_calls_list.append(
                                ToolCall(
                                    id=tc_id,
                                    name=tc_name,
                                    arguments=tc_args,
                                )
                            )
                        # 使用 content_blocks 参数传递工具调用
                        msg_dict["content_blocks"] = tool_calls_list
                    # 处理工具结果消息
                    if hasattr(msg, "tool_call_id") and msg.tool_call_id:
                        msg_dict["role"] = "tool"
                        msg_dict["tool_call_id"] = msg.tool_call_id
                    api_messages.append(msg_dict)
            else:
                api_messages = self._client._convert_messages(messages)
        else:
            api_messages = self._client._convert_messages(messages)

        # 转换工具格式
        openai_tools = self._client._convert_tools(self._tools)

        # 构建请求参数
        request_params = {
            "model": self._client.model_name,
            "messages": api_messages,
            "tools": openai_tools,
        }

        # 添加思考模式参数
        if "thinking" in params:
            request_params["thinking"] = params["thinking"]

        # 添加其他参数
        for key in ["temperature", "max_tokens"]:
            if key in params:
                request_params[key] = params[key]

        logger.info(
            f"[智谱 API] 思考+工具调用 | model={self._client.model_name} | "
            f"thinking={bool(params.get('thinking'))} | "
            f"tools_count={len(self._tools)}"
        )

        try:
            # 在线程池中执行同步调用
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: self._client._client.chat.completions.create(**request_params),
            )

            # 提取响应内容
            choice = response.choices[0]
            message = choice.message

            content = message.content or ""

            # 提取思考内容
            reasoning_content = getattr(message, "reasoning_content", None)

            # 构建 additional_kwargs
            additional_kwargs = {}
            if reasoning_content:
                additional_kwargs["reasoning_content"] = reasoning_content
                logger.info(f"[智谱 API] 思考内容已提取 | len={len(reasoning_content)}")

            # 处理工具调用
            tool_calls = []
            if hasattr(message, "tool_calls") and message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "name": tc.function.name,
                            "args": json.loads(tc.function.arguments),
                        }
                    )
                logger.info(
                    f"[智谱 API] 工具调用 | count={len(tool_calls)} | "
                    f"tools={[tc['name'] for tc in tool_calls]}"
                )

            # 构建 response_metadata
            response_metadata = {
                "model_name": self._client.model_name,
                "finish_reason": choice.finish_reason,
            }
            if hasattr(response, "usage") and response.usage:
                response_metadata["token_usage"] = {
                    "prompt_tokens": response.usage.prompt_tokens,
                    "completion_tokens": response.usage.completion_tokens,
                    "total_tokens": response.usage.total_tokens,
                }

            return AIMessage(
                content=content,
                additional_kwargs=additional_kwargs,
                tool_calls=tool_calls,
                response_metadata=response_metadata,
            )

        except Exception as e:
            logger.error(f"[智谱 API] 思考+工具调用错误 | error={str(e)}")
            self._client._handle_error(e)
