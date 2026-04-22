"""LLM Adapter 中间层 — 统一 LLM 调用抽象与多模型 fallback。

在 LLMCore 和 litellm 之间加一层抽象，支持：
- 统一的 LLMResponse 响应结构
- 非流式和流式两种调用模式
- 多模型 fallback 自动切换
- reasoning_content（thinking）解析
- tool_calls 解析（非流式和流式增量合并）
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

import litellm

litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

_THINK_PATTERN = re.compile(
    r"<think[^>]*>(.*?)</think[^>]*>",
    re.DOTALL,
)
_THINK_PATTERN_NO_GT = re.compile(
    r"<think\s(.*?)</think[^>]*>",
    re.DOTALL,
)


def _extract_thinking_from_content(content: str | None) -> tuple[str | None, str | None]:
    """从 content 中提取 <think/> 标签内容，返回 (thinking_text, cleaned_content)。

    BUG-FIX-fix_20260418_163500_think_extract
    问题根因: MiniMax-M2.7 等推理模型的思考内容包裹在 <think/> 标签中
    混在 content 字段返回，litellm 不会自动映射到 reasoning_content
    修复方案: 手动解析 <think/> 标签，将思考内容与正文分离
    影响范围: 所有未自动映射 reasoning_content 的推理模型

    支持两种标签格式：
    1. 标准 XML: <think\\n...\\n</think/> 或 <think type="x">...</think...>
    2. MiniMax: <think\\n...\\n</think/> (开始标签无 >)

    Args:
        content: LLM 返回的原始 content 文本

    Returns:
        (thinking_text, cleaned_content) 元组
    """
    if not content:
        return None, content

    pattern, matches = _THINK_PATTERN, _THINK_PATTERN.findall(content)
    if not matches:
        pattern, matches = _THINK_PATTERN_NO_GT, _THINK_PATTERN_NO_GT.findall(content)
    if not matches:
        return None, content

    thinking = "\n".join(m.strip() for m in matches if m.strip())
    cleaned = pattern.sub("", content).strip()
    return thinking if thinking else None, cleaned if cleaned else None


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    """统一 LLM 响应结构。

    Attributes:
        text: LLM 响应文本内容
        tool_calls: 解析后的工具调用列表
        thinking_text: 思考过程文本（如 DeepSeek reasoning_content）
        usage: token 用量信息
    """

    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str | None = None
    usage: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------

@runtime_checkable
class LLMAdapter(Protocol):
    """LLM 调用适配器抽象接口。

    所有 LLM 调用实现都应遵循此协议，
    包括直接调用 litellm 的适配器和带 fallback 的适配器。
    """

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            stream: 是否使用流式模式
            on_chunk: 流式回调函数（仅流式模式下使用）
            **kwargs: 其他传递给 litellm 的参数（如 api_base、api_key、temperature 等）

        Returns:
            统一的 LLMResponse 响应结构
        """
        ...

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。

        Args:
            model: LiteLLM 格式的模型标识字符串

        Returns:
            模型是否健康可用
        """
        ...


# ---------------------------------------------------------------------------
# LiteLLM 适配器实现
# ---------------------------------------------------------------------------

class LiteLLMAdapter:
    """基于 litellm 的 LLM 调用适配器。

    封装 litellm.acompletion() 调用，处理非流式和流式两种模式，
    将 litellm 的原始响应转换为统一的 LLMResponse 结构。
    """

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用，支持非流式和流式两种模式。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            stream: 是否使用流式模式
            on_chunk: 流式回调函数
            **kwargs: 其他传递给 litellm 的参数

        Returns:
            统一的 LLMResponse 响应结构
        """
        if stream:
            return await self._call_streaming(
                model, messages, tools=tools, on_chunk=on_chunk, **kwargs
            )
        return await self._call_non_streaming(
            model, messages, tools=tools, **kwargs
        )

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。

        通过发送一个简短的测试请求来验证模型是否可达。

        Args:
            model: LiteLLM 格式的模型标识字符串

        Returns:
            模型是否健康可用
        """
        try:
            response = await litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return bool(response.choices)
        except Exception as exc:
            logger.warning(
                "[LiteLLMAdapter] health_check 失败 model=%s: %s — %s",
                model, type(exc).__name__, exc,
            )
            return False

    async def _call_non_streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """非流式调用 LLM。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            **kwargs: 其他传递给 litellm 的参数

        Returns:
            统一的 LLMResponse 响应结构
        """
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        response = await litellm.acompletion(**call_kwargs)

        choice = response.choices[0]
        result_text = choice.message.content
        tool_calls = self._parse_tool_calls(choice.message.tool_calls)

        # 优先从 reasoning_content 提取思考内容
        thinking_text: str | None = None
        if hasattr(choice.message, "reasoning_content") and choice.message.reasoning_content:
            thinking_text = choice.message.reasoning_content
            if not result_text:
                result_text = thinking_text
                logger.info(
                    "[LiteLLMAdapter] 使用 reasoning_content 作为 result_text (len=%d)",
                    len(result_text),
                )

        # 兜底：当 reasoning_content 为空时，手动从 content 中提取 <think/> 标签
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = _extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[LiteLLMAdapter] 从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    len(thinking_text), len(result_text or ""),
                )

        # 解析 usage 信息
        usage: dict[str, Any] | None = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
            }

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=usage,
        )

    async def _call_streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """流式调用 LLM。

        通过 on_chunk 回调逐块传递结果，同时收集完整响应。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            on_chunk: 流式回调函数
            **kwargs: 其他传递给 litellm 的参数

        Returns:
            统一的 LLMResponse 响应结构
        """
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        response = await litellm.acompletion(**call_kwargs, drop_params=True)

        result_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}
        stream_usage: dict[str, Any] | None = None

        async for chunk in response:
            # 收集流式 usage（通常在最后一个 chunk）
            if hasattr(chunk, "usage") and chunk.usage:
                stream_usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                    "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                }

            if not chunk.choices:
                continue

            delta = chunk.choices[0].delta

            # LiteLLM 统一将各提供商的推理内容映射到 delta.reasoning_content
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                thinking_parts.append(reasoning)
                if on_chunk:
                    on_chunk({"type": "thinking", "content": reasoning})

            # 文本内容（LiteLLM 已自动剥离 <think/> 标签中的内容）
            if delta.content:
                result_parts.append(delta.content)
                if on_chunk:
                    on_chunk({"type": "text", "content": delta.content})

            # 工具调用（流式增量）
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if hasattr(tc, "index") else 0
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "name": "",
                            "arguments": "",
                        }
                    if tc.function:
                        if tc.function.name:
                            tool_calls_map[idx]["name"] += tc.function.name
                        if tc.function.arguments:
                            tool_calls_map[idx]["arguments"] += tc.function.arguments

                if on_chunk:
                    on_chunk({"type": "tool_call", "tool_calls": delta.tool_calls})

        result_text = "".join(result_parts) if result_parts else None
        thinking_text = "".join(thinking_parts) if thinking_parts else None
        tool_calls = self._normalize_tool_calls(tool_calls_map)

        # 兜底：当流式中未收到 reasoning_content 时，从收集的文本中提取 <think/> 标签
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = _extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[LiteLLMAdapter] 流式模式从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    len(thinking_text), len(result_text or ""),
                )

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=stream_usage,
        )

    def _parse_tool_calls(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        """解析非流式响应中的 tool_calls。

        Args:
            raw_tool_calls: LiteLLM 响应中的 tool_calls 对象列表

        Returns:
            标准化的工具调用列表 [{"id": ..., "name": ..., "arguments": ...}]
        """
        if not raw_tool_calls:
            return []

        parsed: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            parsed.append({
                "id": getattr(tc, "id", None) or f"call_{len(parsed)}",
                "name": tc.function.name,
                "arguments": tc.function.arguments,
            })
        return parsed

    def _normalize_tool_calls(
        self, tool_calls_map: dict[int, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """将流式收集的 tool_calls 映射归一化。

        Args:
            tool_calls_map: 索引到工具调用片段的映射

        Returns:
            排序后的标准化工具调用列表
        """
        if not tool_calls_map:
            return []

        result: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            result.append({
                "name": tc["name"],
                "arguments": tc["arguments"],
            })
        return result


# ---------------------------------------------------------------------------
# Fallback 适配器实现
# ---------------------------------------------------------------------------

class FallbackAdapter:
    """带 fallback 的 LLM 调用适配器。

    先尝试 primary 适配器，失败后按顺序尝试 fallback 适配器，
    全部失败则抛出最后一个异常。

    Attributes:
        _primary: 主适配器
        _fallbacks: 备用适配器列表
        _per_adapter_timeout: 单个适配器的调用超时（秒），None 表示不超时
    """

    def __init__(
        self,
        primary: LLMAdapter,
        fallbacks: list[LLMAdapter],
        per_adapter_timeout: float | None = 30.0,
    ) -> None:
        """初始化 FallbackAdapter。

        Args:
            primary: 主适配器
            fallbacks: 备用适配器列表（按优先级排序）
            per_adapter_timeout: 单个适配器的调用超时秒数，默认 30s，None 不限时
        """
        self._primary = primary
        self._fallbacks = fallbacks
        self._per_adapter_timeout = per_adapter_timeout

    async def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用，失败时自动切换到备用适配器。

        先尝试 primary，失败后按顺序尝试 fallbacks，
        全部失败则抛出最后一个异常。

        Args:
            model: LiteLLM 格式的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            stream: 是否使用流式模式
            on_chunk: 流式回调函数
            **kwargs: 其他传递给底层适配器的参数

        Returns:
            统一的 LLMResponse 响应结构

        Raises:
            Exception: 所有适配器都失败时抛出最后一个异常
        """
        last_exc: Exception | None = None

        async def _call_with_timeout(adapter: LLMAdapter) -> LLMResponse:
            """带超时的适配器调用。"""
            coro = adapter.completion(
                model, messages, tools=tools, stream=stream, on_chunk=on_chunk, **kwargs
            )
            if self._per_adapter_timeout is not None:
                return await asyncio.wait_for(coro, timeout=self._per_adapter_timeout)
            return await coro

        # 尝试 primary
        try:
            return await _call_with_timeout(self._primary)
        except asyncio.TimeoutError:
            last_exc = TimeoutError(
                f"primary 调用超时 ({self._per_adapter_timeout}s) model={model}"
            )
            logger.warning("[FallbackAdapter] %s", last_exc)
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "[FallbackAdapter] primary 调用失败 model=%s: %s — %s",
                model, type(exc).__name__, exc,
            )

        # 依次尝试 fallbacks
        for i, fallback in enumerate(self._fallbacks):
            try:
                logger.info(
                    "[FallbackAdapter] 切换到 fallback[%d] model=%s",
                    i, model,
                )
                return await _call_with_timeout(fallback)
            except asyncio.TimeoutError:
                last_exc = TimeoutError(
                    f"fallback[{i}] 调用超时 ({self._per_adapter_timeout}s) model={model}"
                )
                logger.warning("[FallbackAdapter] %s", last_exc)
            except Exception as exc:
                last_exc = exc
                logger.warning(
                    "[FallbackAdapter] fallback[%d] 调用失败 model=%s: %s — %s",
                    i, model, type(exc).__name__, exc,
                )

        # 全部失败
        logger.error(
            "[FallbackAdapter] 所有适配器均失败 model=%s",
            model,
        )
        raise last_exc  # type: ignore[misc]

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。

        先检查 primary，失败则依次检查 fallbacks，
        任何一个可用即返回 True。

        Args:
            model: LiteLLM 格式的模型标识字符串

        Returns:
            是否有至少一个可用适配器
        """
        try:
            if await self._primary.health_check(model):
                return True
        except Exception as exc:
            logger.warning(
                "[FallbackAdapter] primary health_check 失败: %s — %s",
                type(exc).__name__, exc,
            )

        for i, fallback in enumerate(self._fallbacks):
            try:
                if await fallback.health_check(model):
                    logger.info(
                        "[FallbackAdapter] fallback[%d] health_check 通过",
                        i,
                    )
                    return True
            except Exception as exc:
                logger.warning(
                    "[FallbackAdapter] fallback[%d] health_check 失败: %s — %s",
                    i, type(exc).__name__, exc,
                )

        return False
