"""LLM Adapter 中间层 — 统一 LLM 调用抽象与多模型 fallback。

在 LLMCore 和 litellm 之间加一层抽象，支持：
- 统一的 LLMResponse 响应结构
- 非流式和流式两种调用模式
- 多 key 自动切换（KeyPool + litellm Router 内置）
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


def cleanup_litellm_logging() -> None:
    """清理 LiteLLM 内部 LoggingWorker 后台任务。

    LiteLLM 的 LoggingWorker 会创建长期运行的后台 asyncio Task，
    在 asyncio.run() 结束事件循环时这些 Task 不会被正确取消，
    导致 "Task was destroyed but it is pending!" 警告。

    此函数尝试取消这些未完成的 Task，消除关闭时的警告噪音。
    """
    try:
        # litellm 内部可能有多个 logging worker 实例
        from litellm.litellm_core_utils import logging_worker as _lw

        for attr_name in ("_workers", "_instance", "_instances"):
            obj = getattr(_lw, attr_name, None)
            if obj is None:
                continue
            if isinstance(obj, dict):
                for worker in obj.values():
                    _cancel_worker_tasks(worker)
            elif isinstance(obj, list):
                for worker in obj:
                    _cancel_worker_tasks(worker)
            else:
                _cancel_worker_tasks(obj)
    except Exception as exc:
        logger.debug("cleanup_litellm_logging 部分步骤失败（可忽略）: %s", exc)


async def cleanup_litellm_resources() -> None:
    """清理 LiteLLM 所有资源：后台任务 + HTTP 会话。

    在异步上下文中调用（事件循环仍活跃时）。
    """
    cleanup_litellm_logging()
    try:
        from litellm.llms.custom_httpx.async_client_cleanup import (
            close_litellm_async_clients,
        )
        await close_litellm_async_clients()
    except Exception as exc:
        logger.debug("close_litellm_async_clients 失败（可忽略）: %s", exc)


def cleanup_litellm_resources_sync() -> None:
    """同步版本，在事件循环关闭后调用（如 main() finally 块）。"""
    cleanup_litellm_logging()
    try:
        from litellm.llms.custom_httpx.async_client_cleanup import (
            close_litellm_async_clients,
        )
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(close_litellm_async_clients())
        finally:
            loop.close()
    except Exception as exc:
        logger.debug("cleanup_litellm_resources_sync 部分步骤失败（可忽略）: %s", exc)


def _cancel_worker_tasks(worker: Any) -> None:
    """取消单个 worker 的后台任务。"""
    if worker is None:
        return
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    if loop is None or loop.is_closed():
        return
    for attr_name in ("_task", "_background_task", "_loop_task"):
        task = getattr(worker, attr_name, None)
        if task is not None and isinstance(task, asyncio.Task) and not task.done():
            task.cancel()

# 专用 logger：只写文件，不传播到 root（不显示在 CLI）
_diag_logger = logging.getLogger(__name__ + "._diag")
_diag_logger.propagate = False
_stream_logger = logging.getLogger(__name__ + "._stream")
_stream_logger.propagate = False


def _sync_diag_handlers() -> None:
    """将父 logger 的 FileHandler 同步到 _diag_logger。"""
    if _diag_logger.handlers:
        return
    for h in logger.handlers:
        if isinstance(h, logging.FileHandler):
            _diag_logger.addHandler(h)
            _diag_logger.setLevel(logging.DEBUG)

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
        stream_truncated: 流式响应是否被 API 侧超时异常截断
            （如推理模型 thinking 正常但正文极少 token 后 SSE 超时）
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

    @staticmethod
    def _ensure_minimax_role_safety(
        model: str, messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """防御性兜底：确保 minimax 模型不会收到非首位 system 消息。

        根因：MiniMax API 仅允许首位消息为 system role。管道中的
        StreamRepetitionGuard、ThinkingTruncationGuard 等会注入 system 消息，
        _normalize_messages_for_provider 的 Phase 1-4 已做转换，但极端边界
        情况可能遗漏。此方法作为最后一道防线，在 adapter 层拦截。

        Args:
            model: LiteLLM 模型标识字符串（如 "minimax/MiniMax-M2.7"）
            messages: 对话消息列表

        Returns:
            修正后的消息列表（原地修改 + 返回引用）
        """
        # 检测是否为 minimax 模型
        if "minimax" not in model.lower():
            return messages

        needs_fix = False
        for i, msg in enumerate(messages):
            if i > 0 and msg.get("role") == "system":
                needs_fix = True
                break

        if not needs_fix:
            return messages

        for i, msg in enumerate(messages):
            if i > 0 and msg.get("role") == "system":
                msg["role"] = "user"
                msg.pop("name", None)
                logger.warning(
                    "[adapter] Minimax 兜底: 非首位 system→user idx=%d content=%s",
                    i, str(msg.get("content", ""))[:100],
                )
        return messages

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
        # 防御性兜底：确保 minimax 不会收到非法 system 消息
        self._ensure_minimax_role_safety(model, messages)

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
            _prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
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
        _stream_start: float = _time.monotonic()

        # 流式超时：首个 chunk 检测连接是否建立，后续 chunk 防止连接僵死
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", 60))
        inter_chunk_timeout = float(kwargs.pop("inter_chunk_timeout", 300))

        # BUG-FIX-fix_20260606_socket_level_timeout:
        # asyncio.wait_for 的 cancel 无法中断 httpx 在 C 层的 socket recv()，
        # 导致流式调用在 API 不响应时永久卡死。解决：将 inter_chunk_timeout
        # 透传给 litellm → httpx.Timeout(read=N)，在 socket 层面强制超时。
        call_kwargs["timeout"] = inter_chunk_timeout

        stream_repetition = False
        thinking_truncated = False
        _max_thinking_chars = int(
            kwargs.pop("max_thinking_chars", 180000)
        )

        # BUG-FIX-fix_20260529_minimax_thinking_stream:
        # 流式 <think/> 标签状态机。MiniMax 等模型的思考内容通过 <think/> 标签
        # 包裹在 delta.content 中返回（而非 delta.reasoning_content），且标签会
        # 跨多个 chunk 切分。状态机通过 "<think" / "</think" 字符串查找跟踪开/闭状态，
        # 确保 thinking 内容正确路由到 thinking 通道、正文路由到 text 通道。
        _in_think_tag: bool = False

        aiter = response.__aiter__()
        try:
            # 首个 chunk：检测连接是否建立，超时说明被限流或网络异常
            try:
                chunk = await asyncio.wait_for(
                    aiter.__anext__(), timeout=first_chunk_timeout
                )
            except StopAsyncIteration:
                # 空流，直接返回
                return LLMResponse()
            except asyncio.TimeoutError:
                logger.error(
                    "[%s] STREAM TIMEOUT: first chunk 超时 (%.0fs)"
                    " model=%s",
                    type(self).__name__, first_chunk_timeout, model,
                )
                raise litellm.Timeout(
                    message=(
                        "Stream first chunk timeout:"
                        f" no data for {first_chunk_timeout:.0f}s"
                    ),
                    model=model,
                    llm_provider="zai",
                )

            # 边收边处理，保持真正的流式
            # _process_chunk 内联处理每个 chunk
            async def _process_chunk(chunk: Any) -> bool:
                """处理单个 chunk，返回是否应该 break。"""
                nonlocal stream_repetition, _in_think_tag, stream_usage
                # 流式诊断：只写文件，不显示在 CLI
                _chunk_idx = len(result_parts) + len(thinking_parts)
                if _chunk_idx <= 1 or _chunk_idx % 200 == 0:
                    _sync_diag_handlers()
                    if _diag_logger.handlers:
                        _delta = getattr(
                            getattr(chunk, "choices", [None])[0],
                            "delta", None,
                        )
                        _tc = getattr(_delta, "tool_calls", None)
                        _usage = getattr(chunk, "usage", None)
                        if _chunk_idx <= 1 or _tc or _usage:
                            _rc = getattr(_delta, "reasoning_content", None)
                            _ct = getattr(_delta, "content", None)
                            _diag_logger.debug(
                                "[%s] chunk #%d:"
                                " content=%s reasoning=%s"
                                " tc=%s usage=%s",
                                type(self).__name__,
                                _chunk_idx,
                                repr((_ct or "")[:40]),
                                repr((_rc or "")[:40]) if _rc else "-",
                                "Y" if _tc else "-",
                                "Y" if _usage else "-",
                            )
                # 收集流式 usage（通常在最后一个 chunk）
                if hasattr(chunk, "usage") and chunk.usage:
                    _prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
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
                        "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
                    }

                if not chunk.choices:
                    return False

                delta = chunk.choices[0].delta

                # LiteLLM 统一推理内容映射到 delta.reasoning_content
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    thinking_parts.append(reasoning)
                    _stream_logger.debug(
                        "[STREAM][THINKING] #%d +%d chars",
                        len(thinking_parts), len(reasoning),
                    )
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
                        logger.warning(
                            "[%s] 思考内容过长"
                            "(%d>%d chars)，截断",
                            type(self).__name__,
                            thinking_len,
                            _max_thinking_chars,
                        )
                        return True

                # 文本内容：流式 <think/> 状态机处理（MiniMax 等模型）
                if delta.content:
                    content = delta.content

                    if _in_think_tag:
                        # 标签内：检查闭合标签
                        if "</think" in content:
                            close_idx = content.index("</think")
                            _think_part = content[:close_idx]
                            if _think_part:
                                thinking_parts.append(_think_part)
                                if on_chunk:
                                    on_chunk({"type": "thinking", "content": _think_part})
                            _after_close = content[close_idx:]
                            _gt = _after_close.find(">")
                            _rest = _after_close[_gt + 1:] if _gt >= 0 else ""
                            _in_think_tag = False
                            if _rest.strip():
                                result_parts.append(_rest)
                                if on_chunk:
                                    signal = on_chunk({"type": "text", "content": _rest})
                                    if signal == "stop":
                                        stream_repetition = True
                                        return True
                        else:
                            thinking_parts.append(content)
                            _stream_logger.debug(
                                "[STREAM][THINKING] #%d +%d chars",
                                len(thinking_parts), len(content),
                            )
                            if on_chunk:
                                on_chunk({"type": "thinking", "content": content})
                            thinking_len = sum(len(p) for p in thinking_parts)
                            if _max_thinking_chars > 0 and thinking_len > _max_thinking_chars:
                                logger.warning(
                                    "[%s] 思考内容过长 (%d>%d chars)，截断",
                                    type(self).__name__,
                                    thinking_len, _max_thinking_chars,
                                )
                                return True
                    else:
                        # 标签外：检查开标签
                        if "<think" in content:
                            _open_idx = content.index("<think")
                            _before = content[:_open_idx]
                            if _before:
                                result_parts.append(_before)
                                if on_chunk:
                                    on_chunk({"type": "text", "content": _before})
                            _after_open = content[_open_idx:]
                            _gt = _after_open.find(">")
                            _inner = _after_open[_gt + 1:] if _gt >= 0 else ""
                            _in_think_tag = True
                            if "</think" in _inner:
                                _ci = _inner.index("</think")
                                _tp = _inner[:_ci]
                                if _tp:
                                    thinking_parts.append(_tp)
                                    if on_chunk:
                                        on_chunk({"type": "thinking", "content": _tp})
                                _ac = _inner[_ci:]
                                _g2 = _ac.find(">")
                                _rs = _ac[_g2 + 1:] if _g2 >= 0 else ""
                                _in_think_tag = False
                                if _rs.strip():
                                    result_parts.append(_rs)
                                    if on_chunk:
                                        signal = on_chunk({"type": "text", "content": _rs})
                                        if signal == "stop":
                                            stream_repetition = True
                                            return True
                            elif _inner:
                                thinking_parts.append(_inner)
                                _stream_logger.debug(
                                    "[STREAM][THINKING] #%d +%d chars",
                                    len(thinking_parts), len(_inner),
                                )
                                if on_chunk:
                                    on_chunk({"type": "thinking", "content": _inner})
                        else:
                            if on_chunk and thinking_parts:
                                on_chunk({"type": "thinking_end", "content": ""})
                            result_parts.append(content)
                            _stream_logger.debug(
                                "[STREAM][TEXT] #%d +%d chars: %s",
                                len(result_parts), len(content),
                                repr(content[:80]),
                            )
                            if on_chunk:
                                signal = on_chunk(
                                    {"type": "text", "content": content}
                                )
                                if signal == "stop":
                                    stream_repetition = True
                                    logger.warning(
                                        "[%s] 收到 stop 信号，截断流式输出",
                                        type(self).__name__,
                                    )
                                    return True

                # 工具调用（流式增量）
                if delta.tool_calls:
                    # thinking→tool_calls 过渡：发送 thinking_end 确保思考完整关闭后再输出工具卡片
                    if on_chunk and thinking_parts:
                        on_chunk({"type": "thinking_end", "content": ""})
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
                            _stream_logger.debug(
                                "[STREAM][TOOL_CALL] #%d new: id=%s",
                                idx, tool_calls_map[idx]["id"],
                            )
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] += (
                                    tc.function.name
                                )
                                _stream_logger.debug(
                                    "[STREAM][TOOL_CALL] #%d name=%s",
                                    idx, tool_calls_map[idx]["name"],
                                )
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += (
                                    tc.function.arguments
                                )
                                _arg_len = len(
                                    tool_calls_map[idx]["arguments"]
                                )
                                if _arg_len <= 50 or _arg_len % 500 == 0:
                                    _stream_logger.debug(
                                        "[STREAM][TOOL_CALL] #%d args +%d → %d chars",
                                        idx,
                                        len(tc.function.arguments),
                                        _arg_len,
                                    )

                    if on_chunk:
                        on_chunk({
                            "type": "tool_call",
                            "tool_calls": delta.tool_calls,
                        })
                return False

            # 处理首个 chunk
            await _process_chunk(chunk)

            # 后续 chunk：带 inter-chunk 超时，防止连接僵死
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        aiter.__anext__(), timeout=inter_chunk_timeout
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] STREAM TIMEOUT: inter-chunk 超时 (%.0fs)"
                        " model=%s chunks_received=%d",
                        type(self).__name__, inter_chunk_timeout, model,
                        len(result_parts) + len(thinking_parts),
                    )
                    raise litellm.Timeout(
                        message=(
                            "Stream inter-chunk timeout:"
                            f" no data for {inter_chunk_timeout:.0f}s"
                        ),
                        model=model,
                        llm_provider="zai",
                    )
                if await _process_chunk(chunk):
                    break
        finally:
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接
            if hasattr(response, "aclose"):
                await response.aclose()

        result_text = "".join(result_parts) if result_parts else None
        thinking_text = "".join(thinking_parts) if thinking_parts else None
        tool_calls = self._normalize_tool_calls(tool_calls_map)

        # 流式接收完成：记录速度统计
        _stream_elapsed = _time.monotonic() - _stream_start
        _comp_tokens = (stream_usage or {}).get("completion_tokens", 0)
        _speed = (_comp_tokens / _stream_elapsed) if _stream_elapsed > 0 and _comp_tokens else 0
        _stream_logger.debug(
            "[STREAM][DONE] text=%d chars thinking=%d chars "
            "chunks=%d tool_calls=%d "
            "tokens=%d elapsed=%.2fs speed=%.1f tok/s",
            len(result_text or ""), len(thinking_text or ""),
            len(result_parts) + len(thinking_parts),
            len(tool_calls),
            _comp_tokens, _stream_elapsed, _speed,
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
# KeyPool 适配器 — 基于 KeyPool 的多 key 聚合 + RPM 限流
# ---------------------------------------------------------------------------

class KeyPoolAdapter(_BaseLiteLLMAdapter):
    """基于 KeyPool 的 LLM 调用适配器。

    按 API key 做并发控制（一个 key 一个信号量 + RPM + 配额）。

    多 key 场景下：
    - 请求前从 KeyPool 选一个最优 key（余量最多）
    - 通过该 key 的信号量控制并发
    - 成功后记录 usage，429 后冷却该 key
    - 所有 key 共享同一个 litellm.Router 的 fallback 能力

    无 KeyPool 的 provider 回退到 Router 默认行为（不限流）。
    """

    def __init__(
        self,
        router: Any,
        *,
        default_max_concurrent: int = 2,
    ) -> None:
        self._router = router
        self._default_max_concurrent = default_max_concurrent

    def _resolve_provider(self, model: str) -> str:
        """从 model_id 查找 provider 名称。

        优先用 router_factory 的映射表（model_id → provider），
        兜底用 litellm 前缀反查。
        """
        from llm.router_factory import (
            get_key_pool,
            get_provider_for_model,
        )

        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        model_id = model.split("/", 1)[1] if "/" in model else model

        # 直接查映射表
        provider = get_provider_for_model(model_id)
        if provider and get_key_pool(provider):
            return provider
        return ""

    def _extract_model_name(self, kwargs: dict[str, Any]) -> str:
        """从 kwargs 中提取 model_name（去掉 provider 前缀）。"""
        model = kwargs.get("model", "")
        if "/" in model:
            return model.split("/", 1)[1]
        return model

    async def _do_completion(self, **kwargs: Any) -> Any:
        from llm.router_factory import get_key_pool
        from llm.key_pool import KeySlot

        model_str = kwargs.get("model", "")
        provider_name = self._resolve_provider(model_str)
        pool = get_key_pool(provider_name) if provider_name else None

        if pool is None:
            # 无 KeyPool，直接走 Router
            return await self._route_call(**kwargs)

        # 尝试每个可用 key，失败后自动换下一个重试
        max_retries = len(pool.slots)
        last_exc: Exception | None = None

        for attempt in range(max_retries):
            slot: KeySlot = await pool.acquire_slot()
            try:
                key_kwargs = dict(kwargs)
                key_kwargs["api_key"] = slot.api_key
                if slot.api_base:
                    key_kwargs.setdefault("api_base", slot.api_base)

                if key_kwargs.get("stream"):
                    result = await self._direct_call_with_slot(
                        slot=slot, **key_kwargs
                    )
                else:
                    result = await self._direct_call_with_slot(
                        slot=slot, **key_kwargs
                    )

                slot.on_success()
                return result
            except litellm.AuthenticationError as exc:
                # 认证失败：冷却该 key，用其他 key 重试
                slot.on_rate_limit(retry_after=300)
                logger.error(
                    "[KeyPoolAdapter] 认证失败 → key=%s 冷却 300s"
                    "，尝试其他 key (attempt %d/%d): %s",
                    slot.key_id, attempt + 1, max_retries, exc,
                )
                last_exc = exc
                # 不要 raise，继续循环尝试下一个 key
            except litellm.RateLimitError as exc:
                # 429 限流：冷却该 key，尝试其他 key
                slot.on_rate_limit()
                last_exc = exc
            except litellm.BudgetExceededError as exc:
                # 配额耗尽（如"每周/每月使用上限"）：冷却该 key，尝试其他 key
                slot.on_rate_limit(retry_after=3600.0)
                logger.warning(
                    "[KeyPoolAdapter] 配额耗尽 → key=%s 冷却 3600s"
                    "，尝试其他 key (attempt %d/%d): %s",
                    slot.key_id, attempt + 1, max_retries, exc,
                )
                last_exc = exc
            except litellm.Timeout as exc:
                last_exc = exc
                # 超时：不冷却 key，不轮转（不是 key 的问题）
                raise
            except asyncio.CancelledError:
                # 用户取消：不冷却，直接抛
                raise
            except litellm.InternalServerError as exc:
                slot.on_rate_limit()
                logger.warning(
                    "[KeyPoolAdapter] InternalServerError"
                    " → key=%s 冷却: %s",
                    slot.key_id, exc,
                )
                last_exc = exc
            except litellm.BadRequestError as exc:
                # 400：通常是请求参数错误，但如果错误消息包含配额相关关键词
                # （如 DeepSeek 的 "Insufficient Balance"），也应轮转 key
                _msg = str(exc).lower()
                _quota_keywords = (
                    "insufficient", "balance", "quota", "exceeded",
                    "limit", "额度", "上限", "用完", "余额", "不足",
                )
                if any(kw in _msg for kw in _quota_keywords):
                    slot.on_rate_limit(retry_after=3600.0)
                    logger.warning(
                        "[KeyPoolAdapter] 疑似配额耗尽 (400) → key=%s 冷却"
                        "，尝试其他 key (attempt %d/%d): %s",
                        slot.key_id, attempt + 1, max_retries, exc,
                    )
                    last_exc = exc
                else:
                    # 真正的请求参数错误 → 不轮转
                    raise
            except Exception as exc:
                # 其他所有异常（含 PermissionDeniedError、ServiceUnavailableError、
                # APIConnectionError 等 API 错误，以及可能的未分类配额错误）：
                # 冷却当前 key 并尝试下一个。只有所有 key 都失败才最终抛异常。
                slot.on_rate_limit()
                logger.warning(
                    "[KeyPoolAdapter] 未分类错误 → key=%s 冷却 type=%s"
                    "，尝试其他 key (attempt %d/%d): %s",
                    slot.key_id, type(exc).__name__, attempt + 1, max_retries, exc,
                )
                last_exc = exc
            finally:
                slot.release()

        # 所有 key 都试过了
        logger.error(
            "[KeyPoolAdapter] 所有 key 均失败"
            " provider=%s model=%s",
            provider_name, model_str,
        )
        raise last_exc  # type: ignore[misc]

    async def _route_call(self, **kwargs: Any) -> Any:
        """无 KeyPool 时的回退路径，动态获取最新 Router。

        BUG-FIX-fix_20260530_config_not_take_effect:
        问题根因: self._router 是初始化时缓存的旧 Router 实例，
                  前端修改模型配置后 invalidate_all_llm_caches() 清除了模块级单例，
                  但 KeyPoolAdapter 仍持有旧 Router 引用，导致配置不生效。
        修复方案: 每次调用时通过 get_or_create_router() 动态获取最新 Router，
                  若模块级单例已被重置则自动从 YAML 重建。
        影响范围: KeyPoolAdapter 的所有 LLM 调用路径
        修复日期: 2026-05-30
        """
        from llm.router_factory import get_or_create_router
        from config.models import get_model_config_loader

        model_loader = get_model_config_loader()
        router = get_or_create_router(model_loader)
        logger.info(
            "[DIAG][KeyPoolAdapter._route_call] model=%s router_id=%s",
            kwargs.get("model", "?"), id(router),
        )
        return await router.acompletion(**kwargs)

    async def _direct_call_with_slot(
        self, slot: Any, **kwargs: Any
    ) -> Any:
        """用指定 slot 的 key 直接调用 litellm.acompletion。

        不经过 Router，直接构建 litellm 参数，确保使用 slot 的 key。
        """
        from llm.router_factory import (
            _PROVIDER_MAP,
            get_provider_for_model,
        )

        model_id = kwargs.get("model", "")
        # 去掉 litellm 前缀（"zai/glm-5.1" → "glm-5.1"）
        bare_model = model_id.split("/", 1)[1] if "/" in model_id else model_id

        # 查 provider → 构建 litellm 模型字符串
        provider = get_provider_for_model(bare_model)
        prefix = _PROVIDER_MAP.get(provider, provider) if provider else ""
        litellm_model = f"{prefix}/{bare_model}" if prefix else bare_model

        # 构建 kwargs：用 slot 的凭证，去掉 model 让 litellm_params 里的生效
        input_kwargs = {k: v for k, v in kwargs.items() if k not in ("model",)}
        input_kwargs["model"] = litellm_model
        input_kwargs["api_key"] = slot.api_key
        if slot.api_base:
            input_kwargs["api_base"] = slot.api_base

        # 禁用 litellm 内部重试：由 KeyPoolAdapter 自己用不同 key 重试
        input_kwargs["num_retries"] = 0

        return await litellm.acompletion(**input_kwargs)
