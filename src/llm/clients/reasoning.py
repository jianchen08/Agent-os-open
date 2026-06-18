"""
思考模型客户端

支持各种思考模型（Reasoning Models）的统一接口
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx

from src.core.exceptions import LLMException as LLMError
from src.llm.base import LLMClient, LLMResponse, Message, TokenUsage, Tool

logger = logging.getLogger(__name__)


class ReasoningClient(LLMClient):
    """思考模型客户端基类"""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str,
        default_params: dict[str, Any] | None = None,
        reasoning_type: str = "deepseek",
    ):
        """初始化思考模型客户端"""
        # 初始化父类 LLMClient
        provider = f"{reasoning_type}_reasoning"
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            api_base=api_base,
            default_params=default_params,
            provider=provider,
        )
        self.reasoning_type = reasoning_type
        self._http_client = httpx.AsyncClient(
            base_url=api_base,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=300.0,
        )

    async def _generate_internal(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> LLMResponse:
        """内部生成文本实现"""
        try:
            # 转换消息格式
            dict_messages = self._convert_messages_to_dict(messages)

            # 合并参数
            params = {**self.default_params, **kwargs}

            # 构建请求
            request_data = self._build_request(dict_messages, params)

            # 发送请求
            response = await self._http_client.post(
                "/chat/completions", json=request_data
            )
            response.raise_for_status()

            result = response.json()

            # 解析响应
            return self._parse_response(result)

        except Exception as e:
            logger.error(f"思考模型生成失败: {e}")
            raise LLMError(f"思考模型生成失败: {str(e)}")

    async def _stream_internal(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """内部流式生成实现"""
        try:
            # 转换消息格式
            dict_messages = self._convert_messages_to_dict(messages)

            # 合并参数
            params = {**self.default_params, **kwargs}
            params["stream"] = True

            # 构建请求
            request_data = self._build_request(dict_messages, params)

            # 发送流式请求
            async with self._http_client.stream(
                "POST", "/chat/completions", json=request_data
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                            # 提取内容（包括思考内容和正式内容）
                            content, reasoning = self._extract_content_from_chunk(chunk)
                            if content:
                                yield content
                            # 注意：reasoning_content 在流式模式下需要特殊处理
                            # 这里暂时只返回 content，reasoning 通过其他机制传递
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            logger.error(f"思考模型流式生成失败: {e}")
            raise LLMError(f"思考模型流式生成失败: {str(e)}")

    async def _stream_with_reasoning_internal(
        self,
        messages: list[Message],
        **kwargs: Any,
    ) -> AsyncIterator[tuple[str, str]]:
        """
        内部流式生成实现（包含思考内容）

        Yields:
            tuple: (content, reasoning_content) - 内容和思考内容可能为空字符串
        """
        try:
            # 转换消息格式
            dict_messages = self._convert_messages_to_dict(messages)

            # 合并参数
            params = {**self.default_params, **kwargs}
            params["stream"] = True

            # 构建请求
            request_data = self._build_request(dict_messages, params)

            # 发送流式请求
            async with self._http_client.stream(
                "POST", "/chat/completions", json=request_data
            ) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data = line[6:]
                        if data == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data)
                            # 提取内容（包括思考内容和正式内容）
                            content, reasoning = self._extract_content_from_chunk(chunk)
                            # 同时返回 content 和 reasoning
                            yield content, reasoning
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            logger.error(f"思考模型流式生成失败: {e}")
            raise LLMError(f"思考模型流式生成失败: {str(e)}")

    async def _generate_with_tools_internal(
        self,
        messages: list[Message],
        tools: list[Tool],
        **kwargs: Any,
    ) -> LLMResponse:
        """内部带工具调用生成实现"""
        # 将工具转换为 OpenAI 格式
        openai_tools = []
        for tool in tools:
            tool_def = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters if hasattr(tool, "parameters") else {"type": "object", "properties": {}, "required": []},
                },
            }
            openai_tools.append(tool_def)

        logger.debug(f"[ReasoningClient] 使用工具调用模式 | 工具数={len(openai_tools)} | 工具={[t['function']['name'] for t in openai_tools]}")

        # 调用内部生成方法，传递工具参数
        return await self._generate_internal(messages, tools=openai_tools, **kwargs)

    def _convert_messages_to_dict(
        self, messages: list[Message]
    ) -> list[dict[str, str]]:
        """转换消息格式为 API 格式"""
        result = []
        for msg in messages:
            result.append({"role": msg.role, "content": msg.content or ""})
        return result

    def _build_request(
        self, messages: list[dict[str, str]], params: dict[str, Any]
    ) -> dict[str, Any]:
        """
        构建请求数据

        透明传递所有参数，不区分供应商类型。
        客户端应该是通用的参数传递者，配置即所见。
        """
        request_data = {
            "model": self.model_name,
            "messages": messages,
        }

        # 添加工具调用参数
        if "tools" in params and params["tools"]:
            request_data["tools"] = params["tools"]
            logger.debug(f"[_build_request] 添加工具调用 | 工具数={len(params['tools'])}")

        # 透明传递所有其他参数（排除内部框架参数）
        internal_keys = {"tools"}
        for key, value in params.items():
            if key not in request_data and key not in internal_keys:
                request_data[key] = value

        return request_data

    def _parse_response(self, response: dict[str, Any]) -> LLMResponse:
        """解析 API 响应"""
        try:
            choice = response["choices"][0]
            message = choice["message"]

            # 基础响应
            content = message.get("content", "")
            reasoning_content = None
            tool_calls = None

            # 提取思考内容（DeepSeek R1 返回 reasoning_content 字段）
            if self.reasoning_type == "deepseek":
                reasoning_content = message.get("reasoning_content")
                if reasoning_content:
                    logger.debug(
                        f"[DeepSeek] 检测到思考内容，长度: {len(reasoning_content)}"
                    )

            # 解析工具调用（OpenAI 格式）
            if "tool_calls" in message:
                tool_calls_data = message["tool_calls"]
                if tool_calls_data:
                    from src.llm.base import ToolCall

                    tool_calls = []
                    for tc in tool_calls_data:
                        # 提取 arguments，可能是 JSON 字符串或字典
                        args_raw = tc.get("function", {}).get("arguments", {})

                        # 转换 JSON 字符串为字典
                        if isinstance(args_raw, str):
                            try:
                                args_dict = json.loads(args_raw)
                                logger.debug(
                                    f"[ReasoningClient] 转换 arguments JSON 字符串为字典 | 工具={tc.get('function', {}).get('name', '')}"
                                )
                            except json.JSONDecodeError as e:
                                logger.error(
                                    f"[ReasoningClient] arguments JSON 解析失败: {e} | raw={args_raw[:100]}"
                                )
                                args_dict = {}
                        elif isinstance(args_raw, dict):
                            args_dict = args_raw
                        else:
                            logger.warning(
                                f"[ReasoningClient] arguments 类型异常: {type(args_raw)} | 使用空字典"
                            )
                            args_dict = {}

                        tool_calls.append(
                            ToolCall(
                                id=tc.get("id", ""),
                                name=tc.get("function", {}).get("name", ""),
                                arguments=args_dict,
                            )
                        )
                    logger.debug(
                        f"[ReasoningClient] 检测到工具调用 | 数量={len(tool_calls)}"
                    )

            # Token 使用统计
            usage_data = response.get("usage", {})
            usage = TokenUsage(
                prompt_tokens=usage_data.get("prompt_tokens", 0),
                completion_tokens=usage_data.get("completion_tokens", 0),
                total_tokens=usage_data.get("total_tokens", 0),
            )

            # 创建响应对象
            llm_response = LLMResponse(
                content=content,
                usage=usage,
                model=response.get("model", self.model_name),
                finish_reason=choice.get("finish_reason", "stop"),
                tool_calls=tool_calls,
            )

            # 将 reasoning_content 存储在额外属性中，供 reasoning_adapter 使用
            if reasoning_content:
                # 使用 Pydantic 的 model_dump 方法后添加额外字段
                # 注意：这里不能直接修改 LLMResponse，需要使用额外机制
                # 我们通过设置一个特殊属性来传递
                llm_response.__dict__["_reasoning_content"] = reasoning_content

            return llm_response

        except (KeyError, IndexError) as e:
            logger.error(f"解析响应失败: {e}")
            raise LLMError(f"解析响应失败: {str(e)}")

    def _extract_content_from_chunk(self, chunk: dict[str, Any]) -> tuple:
        """
        从流式响应块中提取内容

        Args:
            chunk: 流式响应块

        Returns:
            tuple: (content, reasoning_content) - 始终返回字符串，从不返回 None
        """
        try:
            choice = chunk["choices"][0]
            delta = choice.get("delta", {})
            content = delta.get("content") or ""
            # DeepSeek R1 的思考内容在 reasoning_content 字段
            reasoning_content = delta.get("reasoning_content") or ""
            return content, reasoning_content
        except (KeyError, IndexError):
            return "", ""

    async def close(self):
        """关闭客户端"""
        if self._http_client:
            await self._http_client.aclose()


class DeepSeekReasoningClient(ReasoningClient):
    """DeepSeek R1 思考模型客户端"""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str,
        default_params: dict[str, Any] | None = None,
    ):
        super().__init__(model_name, api_key, api_base, default_params, "deepseek")


class OpenAIReasoningClient(ReasoningClient):
    """OpenAI o1/o3 思考模型客户端"""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str,
        default_params: dict[str, Any] | None = None,
    ):
        super().__init__(model_name, api_key, api_base, default_params, "openai")


class AnthropicReasoningClient(ReasoningClient):
    """Claude 3.7 Sonnet 思考模式客户端"""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        api_base: str,
        default_params: dict[str, Any] | None = None,
    ):
        super().__init__(model_name, api_key, api_base, default_params, "anthropic")
