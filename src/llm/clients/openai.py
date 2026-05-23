"""
OpenAI 客户端

基于 LangChain ChatOpenAI 实现，支持 OpenAI API 和兼容格式的第三方 API
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

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


def _validate_and_fix_messages(messages: list) -> list:
    """
    验证并修复消息序列，确保符合 API 要求

    DeepSeek API 要求：
    - tool 角色的消息必须紧跟在包含 tool_calls 的 assistant 消息之后

    Args:
        messages: 原始消息列表

    Returns:
        修复后的消息列表
    """
    if not messages:
        return messages

    fixed_messages = []
    pending_tool_calls = {}  # 记录待响应的 tool_calls

    for i, msg in enumerate(messages):
        # 获取消息角色
        role = None
        tool_calls = None
        tool_call_id = None

        if hasattr(msg, "role"):
            role = msg.role
        elif hasattr(msg, "type"):
            role = msg.type
        elif isinstance(msg, dict):
            role = msg.get("role")

        # 获取 tool_calls
        if hasattr(msg, "tool_calls"):
            tool_calls = msg.tool_calls
        elif isinstance(msg, dict):
            tool_calls = msg.get("tool_calls")

        # 获取 tool_call_id
        if hasattr(msg, "tool_call_id"):
            tool_call_id = msg.tool_call_id
        elif isinstance(msg, dict):
            tool_call_id = msg.get("tool_call_id")

        # 处理 assistant 消息
        if role == "assistant":
            # 记录此消息中的 tool_calls
            if tool_calls:
                for tc in tool_calls:
                    tc_id = None
                    if isinstance(tc, dict):
                        tc_id = tc.get("id")
                    elif hasattr(tc, "id"):
                        tc_id = tc.id
                    if tc_id:
                        pending_tool_calls[tc_id] = i
            fixed_messages.append(msg)

        # 处理 tool 消息
        elif role == "tool":
            # 检查是否有对应的 tool_call
            if tool_call_id and tool_call_id in pending_tool_calls:
                # 有对应的 tool_call，正常添加
                fixed_messages.append(msg)
                del pending_tool_calls[tool_call_id]
            else:
                # 没有对应的 tool_call，跳过此消息
                logger.warning(
                    f"[_validate_and_fix_messages] 跳过孤立的 tool 消息: "
                    f"tool_call_id={tool_call_id}, index={i}"
                )

        # 其他消息直接添加
        else:
            fixed_messages.append(msg)

    return fixed_messages


class _BoundLLMClient:
    """
    绑定工具后的 LLM 客户端包装器

    提供与 OpenAIClient 相同的接口，但在调用时使用绑定工具的模型
    """

    def __init__(self, client: "OpenAIClient", tools: list[dict[str, Any]]):
        """
        初始化绑定工具的客户端

        Args:
            client: 原始客户端
            tools: OpenAI 格式的工具列表
        """
        self._client = client
        self._tools = tools

    async def ainvoke(self, messages, config: dict[str, Any] | None = None, **kwargs):
        """
        调用绑定工具的模型

        支持思考模式参数通过 config 传递
        """
        # 合并 config 和 kwargs
        if config:
            kwargs.update(config)

        # 检查是否已经是 LangChain 格式
        if messages and hasattr(messages[0], "__class__"):
            module_name = messages[0].__class__.__module__
            if module_name.startswith("langchain"):
                lc_messages = messages
            else:
                lc_messages = messages_to_langchain(messages)
        else:
            lc_messages = messages_to_langchain(messages)

        # 验证并修复消息序列（针对 DeepSeek 等严格 API）
        lc_messages = _validate_and_fix_messages(lc_messages)

        # 绑定工具和其他参数（包括思考模式参数）
        bind_params = {"tools": self._tools}

        # 提取需要绑定的参数（如 thinking 等）
        for key in ["thinking", "temperature", "max_tokens"]:
            if key in kwargs:
                bind_params[key] = kwargs.pop(key)

        model = self._client._chat_model.bind(**bind_params)
        return await model.ainvoke(lc_messages, **kwargs)


class OpenAIClient(LLMClient):
    """OpenAI 客户端 - 基于 LangChain ChatOpenAI"""

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
        初始化 OpenAI 客户端

        Args:
            model_name: 模型名称
            api_key: API 密钥
            api_base: API 基础 URL（用于兼容第三方 API）
            default_params: 默认参数
            provider: 提供商名称（用于并发控制）
            enable_concurrency_control: 是否启用并发控制
        """
        # 检测 provider，如果没指定则从 model_name 推断
        detected_provider = provider or (
            "openai"
            if "openai" in model_name.lower() or "gpt" in model_name.lower()
            else "openai_compatible"
        )

        super().__init__(
            model_name,
            api_key,
            api_base,
            default_params,
            provider=detected_provider,
        )

        # 构建 ChatOpenAI 参数
        chat_params = {
            "model": model_name,
        }

        if api_key:
            chat_params["api_key"] = api_key
        if api_base:
            chat_params["base_url"] = api_base

        # 合并默认参数中的 LangChain 支持的参数
        if default_params:
            for key in ["temperature", "max_tokens", "timeout", "max_retries"]:
                if key in default_params:
                    chat_params[key] = default_params[key]

        self._chat_model = ChatOpenAI(**chat_params)

    def _convert_tools(self, tools: list[Tool]) -> list[dict[str, Any]]:
        """转换工具格式为 OpenAI 格式"""
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
                    id=tc.get("id", ""),
                    name=tc.get("name", ""),
                    arguments=tc.get("args", {}),
                )
                for tc in response.tool_calls
            ]

        # 从 response_metadata 获取 usage 信息
        usage = TokenUsage()
        if hasattr(response, "response_metadata") and response.response_metadata:
            token_usage = response.response_metadata.get("token_usage", {})
            usage = TokenUsage(
                prompt_tokens=token_usage.get("prompt_tokens", 0),
                completion_tokens=token_usage.get("completion_tokens", 0),
                total_tokens=token_usage.get("total_tokens", 0),
            )

        return LLMResponse(
            content=response.content if isinstance(response.content, str) else "",
            tool_calls=tool_calls,
            usage=usage,
            model=model or self.model_name,
            finish_reason=(
                response.response_metadata.get("finish_reason", "stop")
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

        # 过滤掉 LangChain 不支持的参数
        invoke_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        try:
            lc_messages = messages_to_langchain(messages)

            # 验证并修复消息序列（针对 DeepSeek 等严格 API）
            lc_messages = _validate_and_fix_messages(lc_messages)

            # 记录输入日志
            logger.info(
                f"[LLM 请求] model={self.model_name} | messages_count={len(messages)}"
            )
            logger.debug(
                f"[LLM 输入] messages={json.dumps([{'role': m.role, 'content': m.content[:200] + '...' if m.content and len(m.content) > 200 else m.content} for m in messages], ensure_ascii=False)}"
            )

            # 如果有运行时参数，创建新的模型实例
            if invoke_params:
                model = self._chat_model.bind(**invoke_params)
                response = await model.ainvoke(lc_messages)
            else:
                response = await self._chat_model.ainvoke(lc_messages)

            result = self._parse_response(response)

            # 记录输出日志
            logger.info(
                f"[LLM 响应] model={self.model_name} | tokens={{prompt={result.usage.prompt_tokens}, completion={result.usage.completion_tokens}, total={result.usage.total_tokens}}}"
            )
            logger.debug(
                f"[LLM 输出] content={result.content[:500] + '...' if result.content and len(result.content) > 500 else result.content}"
            )

            return result
        except Exception as e:
            logger.error(f"[LLM 错误] model={self.model_name} | error={str(e)}")
            self._handle_error(e)

    async def ainvoke(
        self,
        messages,
        **kwargs,
    ):
        """
        异步调用（LangChain 兼容接口）

        直接返回 LangChain AIMessage 对象，供 LangGraph 使用

        Args:
            messages: 消息列表（可以是 Message 或 BaseMessage）
            **kwargs: 额外参数（包括 thinking、tools 等）

        Returns:
            LangChain AIMessage 对象
        """
        params = self._merge_params(**kwargs)

        # 过滤掉 LangChain 不支持的参数，但保留特定模型需要的参数
        invoke_params = {}
        # 标准 LangChain 参数
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        # 智谱 GLM 思考模式参数（通过 extra_body 传递）
        extra_body = {}
        if "thinking" in params:
            extra_body["thinking"] = params["thinking"]
            logger.info(
                f"[LLM ainvoke] 启用思考模式 | model={self.model_name} | "
                f"thinking={params['thinking']}"
            )

        # 结构化输出参数
        if "response_format" in params:
            response_format = params["response_format"]
            if response_format and isinstance(response_format, dict):
                # 检查是否为有效的 response_format
                rf_type = response_format.get("type")
                if rf_type in ("json_schema", "json_object"):
                    # 使用 extra_body 传递 response_format
                    extra_body["response_format"] = response_format
                    logger.info(
                        f"[LLM ainvoke] 启用结构化输出 | model={self.model_name} | "
                        f"type={rf_type}"
                    )

        # 处理工具参数（关键修复！）
        tools = params.get("tools", [])
        if tools:
            logger.info(
                f"[LLM ainvoke] 检测到工具参数 | "
                f"model={self.model_name} | "
                f"tools_count={len(tools)}"
            )
            # 工具已经在 invoke_params 中通过 bind 传递
            # 所以不需要额外处理，只需确保 bind_params 包含 tools

        try:
            # 检查是否已经是 LangChain 格式
            if messages and hasattr(messages[0], "__class__"):
                module_name = messages[0].__class__.__module__
                if module_name.startswith("langchain"):
                    # 已经是 LangChain 格式，直接使用
                    lc_messages = messages
                else:
                    # 需要转换
                    lc_messages = messages_to_langchain(messages)
            else:
                lc_messages = messages_to_langchain(messages)

            # 验证并修复消息序列（针对 DeepSeek 等严格 API）
            lc_messages = _validate_and_fix_messages(lc_messages)

            # 记录输入日志
            logger.info(
                f"[LLM ainvoke] model={self.model_name} | "
                f"messages_count={len(lc_messages)} | "
                f"thinking={bool(extra_body.get('thinking'))} | "
                f"has_tools={bool(tools)}"
            )

            # 如果有运行时参数或 extra_body，创建新的模型实例
            if invoke_params or extra_body or tools:
                bind_params = {**invoke_params}
                if extra_body:
                    # 将 extra_body 作为额外参数传递给 API
                    bind_params["extra_body"] = extra_body
                # 关键修复：如果有工具，转换为 OpenAI 格式并添加到 bind_params 中
                if tools:
                    # 转换内部 Tool 对象为 OpenAI 格式
                    openai_tools = []
                    for tool in tools:
                        tool_name = getattr(tool, "name", "unknown")
                        parameters = getattr(tool, "parameters", {})

                        # 确保 parameters 有 type 字段
                        if "type" not in parameters:
                            parameters["type"] = "object"
                        if "properties" not in parameters:
                            parameters["properties"] = {}
                        if "required" not in parameters:
                            parameters["required"] = []

                        openai_tools.append(
                            {
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "description": getattr(tool, "description", ""),
                                    "parameters": parameters,
                                },
                            }
                        )

                    bind_params["tools"] = openai_tools
                    logger.info(
                        f"[LLM ainvoke] 转换并绑定工具 | "
                        f"tools_count={len(openai_tools)} | "
                        f"tools={[t['function']['name'] for t in openai_tools]}"
                    )
                model = self._chat_model.bind(**bind_params)
                response = await model.ainvoke(lc_messages)
            else:
                response = await self._chat_model.ainvoke(lc_messages)

            # 记录输出日志
            from src.core.tokenizer import get_token_counter

            token_counter = get_token_counter()
            content_tokens = (
                token_counter.count_tokens(response.content) if response.content else 0
            )
            has_tool_calls = hasattr(response, "tool_calls") and bool(
                response.tool_calls
            )
            logger.info(
                f"[LLM ainvoke] model={self.model_name} | "
                f"content_tokens={content_tokens} | "
                f"has_tool_calls={has_tool_calls}"
            )

            return response
        except Exception as e:
            logger.error(f"[LLM ainvoke 错误] model={self.model_name} | error={str(e)}")
            self._handle_error(e)

    def bind_tools(self, tools: list[Tool]):
        """
        绑定工具（LangChain 兼容接口）

        返回绑定工具后的模型实例

        Args:
            tools: 工具列表

        Returns:
            绑定工具后的模型（实际上是 self，但包装了 bind 调用）
        """
        openai_tools = self._convert_tools(tools)
        return _BoundLLMClient(self, openai_tools)

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

            # 验证并修复消息序列（针对 DeepSeek 等严格 API）
            lc_messages = _validate_and_fix_messages(lc_messages)

            # 记录输入日志
            logger.info(
                f"[LLM 流式请求] model={self.model_name} | messages_count={len(lc_messages)}"
            )
            logger.debug(
                f"[LLM 流式输入] messages={json.dumps([{'role': m.role, 'content': m.content[:200] + '...' if m.content and len(m.content) > 200 else m.content} for m in messages], ensure_ascii=False)}"
            )

            if invoke_params:
                model = self._chat_model.bind(**invoke_params)
            else:
                model = self._chat_model

            chunk_count = 0
            async for chunk in model.astream(lc_messages):
                if chunk.content:
                    chunk_count += 1
                    yield chunk.content

            logger.info(
                f"[LLM 流式响应完成] model={self.model_name} | chunks={chunk_count}"
            )

        except Exception as e:
            logger.error(f"[LLM 流式错误] model={self.model_name} | error={str(e)}")
            self._handle_error(e)

    async def stream_with_tools(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs,
    ) -> AsyncIterator[str]:
        """带工具调用的流式生成

        返回格式：
        - 普通文本直接 yield
        - 工具调用 yield 特殊格式: "<<<TOOL_CALL:{json}>>>"
        """
        params = self._merge_params(**kwargs)

        invoke_params = {}
        for key in ["temperature", "max_tokens"]:
            if key in params:
                invoke_params[key] = params[key]

        try:
            lc_messages = messages_to_langchain(messages)

            # 验证并修复消息序列（针对 DeepSeek 等严格 API）
            lc_messages = _validate_and_fix_messages(lc_messages)

            openai_tools = self._convert_tools(tools)

            # 记录输入日志
            tool_names = [t.name for t in tools]
            logger.info(
                f"[LLM 流式工具请求] model={self.model_name} | messages_count={len(messages)} | tools={tool_names}"
            )

            # 检查 system prompt 完整性
            for msg in messages:
                if msg.role == "system":
                    logger.info(
                        f"[LLM] System prompt 完整性检查 | total_length={len(msg.content)} | first_100={msg.content[:100]}... | last_100=...{msg.content[-100:]}"
                    )

            # 绑定工具
            model_with_tools = self._chat_model.bind(tools=openai_tools)

            if invoke_params:
                model_with_tools = model_with_tools.bind(**invoke_params)

            chunk_count = 0
            has_content = False

            # [修复] 收集完整的消息（用于检查工具调用）
            # 流式处理时，最后一个chunk包含完整信息
            final_message = None

            async for chunk in model_with_tools.astream(lc_messages):
                if isinstance(chunk, AIMessage):
                    final_message = chunk  # 保存最后一个消息
                    from src.core.tokenizer import get_token_counter

                    token_counter = get_token_counter()
                    if chunk.content:
                        has_content = True
                        chunk_count += token_counter.count_tokens(chunk.content)
                        logger.debug(
                            f"[LLM 流式工具] yield content chunk | tokens={token_counter.count_tokens(chunk.content)}"
                        )
                        yield chunk.content

            # 检查是否有工具调用（在最后一个消息中）
            if (
                final_message
                and hasattr(final_message, "tool_calls")
                and final_message.tool_calls
            ):
                tool_calls_data = []
                for tc in final_message.tool_calls:
                    tool_call_data = {
                        "id": tc.id or "",
                        "name": tc.name or "",
                        "args": tc.args if isinstance(tc.args, dict) else {},
                    }
                    tool_calls_data.append(tool_call_data)

                # [修复] 如果没有文本内容，先 yield 一个占位符，让前端知道正在处理
                if not has_content:
                    yield "🔧"

                # yield 工具调用特殊标记（WebSocket 可以检测这个格式）
                tool_call_json = json.dumps(tool_calls_data, ensure_ascii=False)
                yield f"\n\n<<<TOOL_CALL:{tool_call_json}>>>"

                logger.info(
                    f"[LLM 流式工具调用] model={self.model_name} | tool_calls={tool_call_json}"
                )

            logger.info(
                f"[LLM 流式工具响应完成] model={self.model_name} | chunks={chunk_count}"
            )

        except Exception as e:
            logger.error(f"[LLM 流式工具错误] model={self.model_name} | error={str(e)}")
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

        # 智谱 GLM 思考模式参数（通过 extra_body 传递）
        extra_body = {}
        if "thinking" in params:
            extra_body["thinking"] = params["thinking"]
            logger.info(
                f"[LLM 工具请求] 启用思考模式 | model={self.model_name} | "
                f"thinking={params['thinking']}"
            )

        # 结构化输出参数
        if "response_format" in params:
            response_format = params["response_format"]
            if response_format and isinstance(response_format, dict):
                rf_type = response_format.get("type")
                if rf_type in ("json_schema", "json_object"):
                    extra_body["response_format"] = response_format
                    logger.info(
                        f"[LLM 工具请求] 启用结构化输出 | model={self.model_name} | "
                        f"type={rf_type}"
                    )

        try:
            lc_messages = messages_to_langchain(messages)

            # 验证并修复消息序列（针对 DeepSeek 等严格 API）
            lc_messages = _validate_and_fix_messages(lc_messages)

            openai_tools = self._convert_tools(tools)

            # 记录输入日志
            tool_names = [t.name for t in tools]
            logger.info(
                f"[LLM 工具请求] model={self.model_name} | "
                f"messages_count={len(lc_messages)} | tools={tool_names} | "
                f"thinking={bool(extra_body.get('thinking'))}"
            )
            logger.debug(
                f"[LLM 工具输入] messages={json.dumps([{'role': m.role, 'content': m.content[:200] + '...' if m.content and len(m.content) > 200 else m.content} for m in messages], ensure_ascii=False)}"
            )
            logger.debug(
                f"[LLM 工具定义] tools={json.dumps(openai_tools, ensure_ascii=False)}"
            )

            # 绑定工具和参数
            bind_params = {"tools": openai_tools}
            if invoke_params:
                bind_params.update(invoke_params)
            if extra_body:
                bind_params["extra_body"] = extra_body

            model_with_tools = self._chat_model.bind(**bind_params)

            response = await model_with_tools.ainvoke(lc_messages)
            result = self._parse_response(response)

            # 记录输出日志
            from src.core.tokenizer import get_token_counter

            token_counter = get_token_counter()
            if result.tool_calls:
                tool_call_info = [
                    {"name": tc.name, "args": tc.arguments} for tc in result.tool_calls
                ]
                logger.info(
                    f"[LLM 工具响应] model={self.model_name} | tool_calls={json.dumps(tool_call_info, ensure_ascii=False)}"
                )
            else:
                logger.info(
                    f"[LLM 工具响应] model={self.model_name} | no_tool_calls | content_tokens={token_counter.count_tokens(result.content) if result.content else 0}"
                )
            logger.debug(
                f"[LLM 工具输出] content={result.content[:500] + '...' if result.content and len(result.content) > 500 else result.content}"
            )

            return result
        except Exception as e:
            logger.error(f"[LLM 工具错误] model={self.model_name} | error={str(e)}")
            self._handle_error(e)

    def as_langchain(self) -> BaseChatModel:
        """获取底层 LangChain ChatOpenAI 实例"""
        return self._chat_model

    # ============================================
    # 兼容旧接口的方法
    # ============================================

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """转换消息格式（兼容旧代码）"""
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
