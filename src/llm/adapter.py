"""LLM Adapter 中间层 — 统一 LLM 调用抽象与多模型 fallback。

在 LLMCore 和 litellm 之间加一层抽象，支持：
- 统一的 LLMResponse 响应结构
- 非流式和流式两种调用模式
- 多模型 fallback 自动切换（FallbackAdapter / Router 内置）
- reasoning_content（thinking）解析
- tool_calls 解析（非流式和流式增量合并）
- 自适应并发控制：根据限流信号动态调整并发 1-3
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
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
        stream_repetition: 流式输出是否被检测为重复而截断
        thinking_truncated: 思考内容是否因过长被截断
    """

    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str | None = None
    usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False


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
# 基类 — 共享响应解析逻辑
# ---------------------------------------------------------------------------

class _BaseLiteLLMAdapter:
    """共享的 LLM 响应解析逻辑。

    子类只需实现 _do_completion() 提供实际的 API 调用入口，
    基类负责非流式/流式调用编排和响应解析。
    """

    async def _do_completion(self, **kwargs: Any) -> Any:
        """执行实际的 LLM API 调用，子类必须覆写。"""
        raise NotImplementedError

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
        """执行 LLM 调用，支持非流式和流式两种模式。"""
        if stream:
            return await self._call_streaming(
                model, messages, tools=tools, on_chunk=on_chunk, **kwargs
            )
        return await self._call_non_streaming(
            model, messages, tools=tools, **kwargs
        )

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。"""
        try:
            response = await self._do_completion(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return bool(response.choices)
        except Exception as exc:
            logger.warning(
                "[%s] health_check 失败 model=%s: %s — %s",
                type(self).__name__, model, type(exc).__name__, exc,
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
        """非流式调用 LLM。"""
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        response = await self._do_completion(**call_kwargs)

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
                    "[%s] 使用 reasoning_content 作为 result_text (len=%d)",
                    type(self).__name__, len(result_text),
                )

        # 兜底：当 reasoning_content 为空时，手动从 content 中提取 <think/> 标签
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = _extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[%s] 从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    type(self).__name__,
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
        """流式调用 LLM。"""
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        response = await self._do_completion(**call_kwargs, drop_params=True)

        result_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}
        stream_usage: dict[str, Any] | None = None

        # 流式 chunk 超时 — 用 asyncio.wait_for 真正中断等待
        # 首个 chunk 超时：60s 内无数据 → 可能被限流
        # 后续 chunk 超时：120s 内无数据 → 网络异常
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", 60))
        inter_chunk_timeout = float(kwargs.pop("inter_chunk_timeout", 120))

        stream_repetition = False
        thinking_truncated = False
        _max_thinking_chars = int(
            kwargs.pop("max_thinking_chars", 30000)
        )
        last_chunk_time: float | None = None

        # 用 async iterator + wait_for 逐 chunk 等待，超时即中断
        # try/finally 确保超时或异常时关闭 iterator，释放 HTTP 连接
        aiter = response.__aiter__()
        try:
            while True:
                current_timeout = (
                    first_chunk_timeout
                    if last_chunk_time is None
                    else inter_chunk_timeout
                )
                try:
                    chunk = await asyncio.wait_for(
                        aiter.__anext__(), timeout=current_timeout
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    if last_chunk_time is None:
                        raise litellm.Timeout(
                            message=(
                                "Stream first chunk timeout:"
                                f" no data for {first_chunk_timeout:.0f}s"
                            ),
                            model=model,
                            llm_provider="zai",
                        )
                    else:
                        raise litellm.Timeout(
                            message=(
                                "Stream chunk timeout:"
                                f" no data for {inter_chunk_timeout:.0f}s"
                            ),
                            model=model,
                            llm_provider="zai",
                        )
                last_chunk_time = _time.monotonic()
                # 收集流式 usage（通常在最后一个 chunk）
                if hasattr(chunk, "usage") and chunk.usage:
                    stream_usage = {
                        "prompt_tokens": getattr(
                            chunk.usage, "prompt_tokens", 0
                        ) or 0,
                        "completion_tokens": getattr(
                            chunk.usage, "completion_tokens", 0
                        ) or 0,
                        "total_tokens": getattr(
                            chunk.usage, "total_tokens", 0
                        ) or 0,
                    }

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta

                # LiteLLM 统一推理内容映射到 delta.reasoning_content
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_parts.append(reasoning)
                    if on_chunk:
                        on_chunk(
                            {"type": "thinking", "content": reasoning}
                        )
                    # 思考内容过长 → 截断
                    thinking_len = sum(
                        len(p) for p in thinking_parts
                    )
                    if (
                        _max_thinking_chars > 0
                        and thinking_len > _max_thinking_chars
                    ):
                        thinking_truncated = True
                        logger.warning(
                            "[%s] 思考内容过长"
                            "(%d>%d chars)，截断",
                            type(self).__name__,
                            thinking_len,
                            _max_thinking_chars,
                        )
                        break

                # 文本内容
                if delta.content:
                    result_parts.append(delta.content)
                    if on_chunk:
                        signal = on_chunk(
                            {"type": "text", "content": delta.content}
                        )
                        if signal == "stop":
                            stream_repetition = True
                            logger.warning(
                                "[%s] "
                                "收到 stop 信号，截断流式输出",
                                type(self).__name__,
                            )
                            break

                # 工具调用（流式增量）
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = (
                            tc.index if hasattr(tc, "index") else 0
                        )
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": (
                                    getattr(tc, "id", None)
                                    or f"tc_{idx}_{id(tool_calls_map)}"
                                ),
                                "name": "",
                                "arguments": "",
                            }
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] += (
                                    tc.function.name
                                )
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += (
                                    tc.function.arguments
                                )

                    if on_chunk:
                        on_chunk({
                            "type": "tool_call",
                            "tool_calls": delta.tool_calls,
                        })
        finally:
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接
            if hasattr(response, "aclose"):
                await response.aclose()

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
                    "[%s] 流式模式从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    type(self).__name__,
                    len(thinking_text), len(result_text or ""),
                )

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=stream_usage,
            stream_repetition=stream_repetition,
            thinking_truncated=thinking_truncated,
        )

    def _parse_tool_calls(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        """解析非流式响应中的 tool_calls。"""
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
        """将流式收集的 tool_calls 映射归一化。"""
        if not tool_calls_map:
            return []

        result: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            result.append({
                "id": tc.get("id") or f"call_{idx}",
                "name": tc["name"],
                "arguments": tc["arguments"],
            })
        return result


# ---------------------------------------------------------------------------
# LiteLLM 适配器 — 直接调用 litellm.acompletion()
# ---------------------------------------------------------------------------

class LiteLLMAdapter(_BaseLiteLLMAdapter):
    """基于 litellm.acompletion() 的 LLM 调用适配器。

    直接调用 litellm 的 acompletion 函数，不经过 Router。
    适用于不需要并发控制的场景或测试环境。
    """

    async def _do_completion(self, **kwargs: Any) -> Any:
        """调用 litellm.acompletion()。"""
        return await litellm.acompletion(**kwargs)


# ---------------------------------------------------------------------------
# Router 适配器 — 通过 litellm.Router 调用（支持并发控制）
# ---------------------------------------------------------------------------

class RouterAdapter(_BaseLiteLLMAdapter):
    """基于 litellm.Router 的 LLM 调用适配器。

    通过 Router 路由到预注册的模型，内置：
    - asyncio.Semaphore 并发控制（per-deployment）
    - fallback 自动切换
    - 重试和 cooldown

    Attributes:
        _router: litellm.Router 实例
    """

    def __init__(self, router: Any) -> None:
        self._router = router

    async def _do_completion(self, **kwargs: Any) -> Any:
        """调用 router.acompletion()。"""
        return await self._router.acompletion(**kwargs)


# ---------------------------------------------------------------------------
# 自适应并发信号量 — 根据限流信号在 min-max 间动态调整
# ---------------------------------------------------------------------------

class _AdaptiveSemaphore:
    """可动态调整容量的异步信号量。

    根据限流和超时信号在 min_capacity ~ max_capacity 之间自动调整并发上限：
    - 收到 RateLimitError → 立即降到最低
    - 连续超时 → 逐步降低
    - 持续成功 → 每 RECOVERY_INTERVAL 秒恢复 1 级

    线程安全，可在多协程间共享。
    """

    RECOVERY_INTERVAL = 90.0  # 成功运行多久后提升 1 级并发
    RECOVERY_SUCCESS_THRESHOLD = 3  # 恢复前至少需要的连续成功次数

    def __init__(
        self,
        initial: int = 2,
        min_capacity: int = 1,
        max_capacity: int = 3,
    ) -> None:
        self._capacity = initial
        self._min = min_capacity
        self._max = max_capacity
        self._count = 0
        self._changed = asyncio.Event()
        self._changed.set()

        self._consecutive_successes = 0
        self._last_decrease_time: float = 0.0

    @property
    def capacity(self) -> int:
        return self._capacity

    async def acquire(self) -> None:
        while self._count >= self._capacity:
            self._changed.clear()
            if self._count >= self._capacity:
                await self._changed.wait()
            else:
                break
        self._count += 1
        if self._count >= self._capacity:
            self._changed.clear()

    def release(self) -> None:
        self._count = max(0, self._count - 1)
        self._changed.set()

    def on_rate_limit(self) -> int:
        """限流信号：立即降到最低，返回新容量。"""
        old = self._capacity
        self._capacity = self._min
        self._consecutive_successes = 0
        self._last_decrease_time = _time.monotonic()
        if old != self._capacity:
            logger.warning(
                "[AdaptiveConcurrency] 限流降级: %d → %d", old, self._capacity,
            )
        return self._capacity

    def on_timeout(self) -> int:
        """超时信号：降 1 级（不低于最低），返回新容量。"""
        old = self._capacity
        self._capacity = max(self._min, self._capacity - 1)
        self._consecutive_successes = 0
        self._last_decrease_time = _time.monotonic()
        if old != self._capacity:
            logger.info(
                "[AdaptiveConcurrency] 超时降级: %d → %d", old, self._capacity,
            )
        return self._capacity

    def on_success(self) -> int:
        """成功信号：连续成功足够多且间隔够长则恢复 1 级，返回新容量。"""
        self._consecutive_successes += 1
        now = _time.monotonic()
        if (
            self._capacity < self._max
            and self._consecutive_successes >= self.RECOVERY_SUCCESS_THRESHOLD
            and (now - self._last_decrease_time) >= self.RECOVERY_INTERVAL
        ):
            old = self._capacity
            self._capacity = min(self._max, self._capacity + 1)
            self._consecutive_successes = 0
            self._changed.set()  # 唤醒等待者
            logger.info(
                "[AdaptiveConcurrency] 成功恢复: %d → %d", old, self._capacity,
            )
        return self._capacity


class AdaptiveRouterAdapter(_BaseLiteLLMAdapter):
    """带自适应并发的 Router 适配器。

    在 RouterAdapter 外层包一层自适应并发控制：
    - 每个模型组（model_name）独立的 _AdaptiveSemaphore
    - 限流/超时时降低并发，成功时逐步恢复
    - 信号量容量在 min_capacity ~ max_capacity 间变化

    Attributes:
        _router: litellm.Router 实例
        _semaphores: 模型组 → 自适应信号量
        _min_capacity: 最低并发
        _max_capacity: 最高并发
        _default_capacity: 初始并发
    """

    def __init__(
        self,
        router: Any,
        *,
        min_capacity: int = 1,
        max_capacity: int = 3,
        default_capacity: int = 2,
    ) -> None:
        self._router = router
        self._min_capacity = min_capacity
        self._max_capacity = max_capacity
        self._default_capacity = default_capacity
        self._semaphores: dict[str, _AdaptiveSemaphore] = {}

    def _get_semaphore(self, model_name: str) -> _AdaptiveSemaphore:
        if model_name not in self._semaphores:
            self._semaphores[model_name] = _AdaptiveSemaphore(
                initial=self._default_capacity,
                min_capacity=self._min_capacity,
                max_capacity=self._max_capacity,
            )
        return self._semaphores[model_name]

    def _extract_model_name(self, kwargs: dict[str, Any]) -> str:
        """从 kwargs 中提取 model_name（去掉 provider 前缀）。"""
        model = kwargs.get("model", "")
        # "zai/glm-5.1" → "glm-5.1"
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    async def _do_completion(self, **kwargs: Any) -> Any:
        model_name = self._extract_model_name(kwargs)
        sem = self._get_semaphore(model_name)
        await sem.acquire()
        try:
            result = await self._router.acompletion(**kwargs)
            sem.on_success()
            return result
        except litellm.RateLimitError:
            sem.on_rate_limit()
            raise
        except litellm.Timeout:
            sem.on_timeout()
            raise
        except Exception:
            raise
        finally:
            sem.release()


# ---------------------------------------------------------------------------
# Fallback 适配器实现（保留向后兼容）
# ---------------------------------------------------------------------------

class FallbackAdapter:
    """带 fallback 的 LLM 调用适配器。

    先尝试 primary 适配器，失败后按顺序尝试 fallback 适配器，
    全部失败则抛出最后一个异常。

    注意：使用 litellm.Router 后，fallback 由 Router 内置处理，
    此适配器不再需要。
    """

    def __init__(
        self,
        primary: LLMAdapter,
        fallbacks: list[LLMAdapter],
        per_adapter_timeout: float | None = 30.0,
    ) -> None:
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
        """执行 LLM 调用，失败时自动切换到备用适配器。"""
        last_exc: Exception | None = None

        async def _call_with_timeout(adapter: LLMAdapter) -> LLMResponse:
            coro = adapter.completion(
                model, messages, tools=tools, stream=stream, on_chunk=on_chunk, **kwargs
            )
            if self._per_adapter_timeout is not None:
                return await asyncio.wait_for(coro, timeout=self._per_adapter_timeout)
            return await coro

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

        logger.error(
            "[FallbackAdapter] 所有适配器均失败 model=%s",
            model,
        )
        raise last_exc  # type: ignore[misc]

    async def health_check(self, model: str) -> bool:
        """检查模型是否可用。"""
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
