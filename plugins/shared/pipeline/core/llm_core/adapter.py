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
import contextlib
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import litellm

# 诊断与审计基础设施（task_kernel_cleanup_and_split 3b：自本模块拆出，
# logger 名保持 "adapter.*"，见 _diagnostics.py）。
from _diagnostics import (
    _diag_logger,
    _log_prompt_body,
    _stream_logger,
    _sync_diag_handlers,
)

# 提供者适配插件注册表（3a：MiniMax 角色修正 / DeepSeek extra_body 透传 /
# <think/> 提取均按模型名分发到 llm_provider_* 插件，llm_core 不绑定提供者）。
from _provider_registry import apply_pre_send, extract_thinking_from_content
from error_classifier import ErrorKind, classify_error
from stream_watchdog import StreamHardTimeout

litellm.suppress_debug_info = True
litellm.set_verbose = False
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# aclose 超时上限（流式连接关闭）：正常关闭一个 HTTP 连接仅需毫秒级，超过该阈值
# 说明底层 socket（常为 Windows ProactorEventLoop 上的 SSL 流）已半死——服务端发了
# FIN 但本地 SSL shutdown 握手等不到响应，httpx/httpcore 的 aclose 会永久阻塞。
# 此时放弃优雅关闭，让协程返回，残留 socket（CLOSE_WAIT）交由 GC/OS 回收。
# 选 10s 是远大于健康关闭耗时、又远小于让管道僵死的可忍受时长。
_ACLOSE_TIMEOUT_SECONDS: float = 10.0

# 后台残留任务登记表：_await_with_escape 超时放弃等待后，被取消的协程若吞掉
# CancelledError 继续挂起（litellm 半死连接场景），task 会留在后台运行。
# 持有强引用 + done 回调自动清理，避免 task 被 GC 时触发 "Task was destroyed
# but it is pending" 告警；同时给上层 finally 的 aclose 兜底机会去强制关闭底层连接。
_background_tasks: set[asyncio.Task[Any]] = set()


def _track_background_task(task: asyncio.Task[Any]) -> None:
    """登记后台任务并绑定自动清理（完成/取消/异常均移除）。

    同时消费 task 异常：被放弃的协程最终异常时，若不取 exception() 会触发
    "Task exception was never retrieved" 告警（done 回调里取一次即消费）。
    """
    _background_tasks.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _background_tasks.discard(t)
        if not t.cancelled():
            with contextlib.suppress(Exception):
                t.exception()  # 消费异常，避免 "never retrieved" 告警

    task.add_done_callback(_on_done)


async def _await_with_escape(
    coro: Any,
    timeout: float,
    *,
    what: str,
) -> Any:
    """带超时等待协程，超时即抛错。

    asyncio.wait 的 timeout 依赖事件循环调度——若协程内部同步阻塞冻住事件循环，
    timeout 回调永远不执行。加独立线程诊断：到点如果 task 还没完成，独立线程
    直接打日志（不依赖事件循环），证明「超时确实该触发但事件循环冻住了」。
    """
    import threading  # noqa: PLC0415
    task = asyncio.ensure_future(coro)
    _track_background_task(task)

    # 独立线程诊断：到点检查 task 是否完成
    def _diag_check() -> None:
        if not task.done():
            logger.error(
                "[_await_with_escape] 独立线程诊断：%.0fs 到点 task 仍未完成 | what=%s "
                "—— asyncio.wait 的 timeout 可能因事件循环冻结而失效",
                timeout, what,
            )

    diag_timer = threading.Timer(timeout, _diag_check)
    diag_timer.daemon = True
    diag_timer.start()

    done, _pending = await asyncio.wait({task}, timeout=timeout)
    diag_timer.cancel()
    if not done:
        logger.error(
            "[_await_with_escape] 超时！cancel task | what=%s timeout=%.0fs",
            what, timeout,
        )
        task.cancel()
        raise asyncio.TimeoutError(f"{what} 超时 {timeout:.0f}s")
    return task.result()


class _ThreadedStreamBridge:
    """跨线程流桥接：worker 线程迭代 litellm 流，主循环从线程安全队列取 chunk。

    背景（生产 2026-08-05）：
    - 17:05:34 litellm.acompletion 卡死 36 分钟：litellm 内部事件循环线程同步
      阻塞冻结主事件循环，asyncio 层超时全部失效 → litellm 移入独立线程。
    - 20:08/20:33:59 首 token 超时修复后出现 "attached to a different loop" /
      "Event loop is closed"：CustomStreamWrapper 绑定 worker 线程的 loop，主循环
      await 它的 __anext__ 会跨 loop 报错 → 流式迭代也留在 worker 线程，
      chunk 经 queue.Queue（线程安全）送回主循环。

    主循环侧接口与 CustomStreamWrapper 对齐：
    - __aiter__/__anext__：从队列取 chunk（StopAsyncIteration 表示流结束）
    - aclose()：通知 worker 关闭底层流（半死连接时不再等 worker，直接返回）
    """

    def __init__(
        self,
        *,
        queue: Any,
        done_evt: Any,
        exc_box: list[BaseException],
        close_evt: Any,
        completion_stream: Any = None,
    ) -> None:
        self._queue = queue
        self._done_evt = done_evt
        self._exc_box = exc_box
        self._close_evt = close_evt
        # 透传底层 completion_stream（心跳诊断读 is_closed 用）；worker 可能
        # 尚未返回流对象时先为 None，worker 完成填充。
        self.completion_stream = completion_stream

    def __aiter__(self) -> _ThreadedStreamBridge:
        return self

    async def __anext__(self) -> Any:
        # 短轮询线程安全队列（不依赖任何事件循环的跨 loop 操作）
        while True:
            if self._exc_box:
                raise self._exc_box[0]
            if self._done_evt.is_set() and self._queue.empty():
                raise StopAsyncIteration
            try:
                return self._queue.get_nowait()
            except Exception:
                await asyncio.sleep(0.05)

    async def aclose(self) -> None:
        """通知 worker 关闭底层流。

        不等待 worker 完成（半死连接会让 aclose 挂起）：设 close_evt 后立即
        返回，worker 线程是 daemon，残留由进程退出回收。上层 finally 已有
        _await_with_escape 兜底，这里保持同步契约（毫秒级返回）。
        """
        self._close_evt.set()


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
        finish_reason: LLM 返回的结束原因（stop/length/tool_calls…）。
            ``length`` 表示因命中 max_tokens 被截断，此时 tool_call 的
            arguments JSON 可能不完整，下游需据此识别并处理截断。
    """

    text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    thinking_text: str | None = None
    usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
    finish_reason: str | None = None


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


@dataclass
class _StreamState:
    """流式调用跨方法共享的可变累积状态。

    ``_call_streaming`` 按职责拆分为多个私有辅助方法后，原闭包/局部可变状态统一由本
    dataclass 持有并在方法间传递（对象属性原地修改），使各方法无需 ``nonlocal`` 即可
    读写同一份状态——行为与历史单体实现的闭包完全等价。

    Attributes:
        result_parts: 正文文本片段（按 chunk 顺序累积）。
        thinking_parts: 思考内容片段（reasoning_content / ``<think/>`` 标签内容）。
        tool_calls_map: 流式 tool_calls 增量按 index 合并的映射。
        stream_usage: 最后一次收到的流式 usage（通常在末尾 chunk）。
        stream_repetition: 是否因 ``on_chunk`` 返回 ``"stop"``（重复检测）而截断。
        thinking_truncated: 思考内容是否因超过 ``max_thinking_chars`` 被截断。
        finish_reason: LLM 返回的结束原因（由接收端点诊断捕获）。
        stream_start: 流式消费起始 monotonic 时间（速度统计用）。
        last_chunk_monotonic: 上个 chunk 到达的 monotonic 时间（心跳量化静默时长用）。
        chunks_received: 累计收到的 chunk 数（心跳/超时日志用）。
        recv_seq: 接收端点诊断序号（统计 tool_calls chunk 到达次数）。
        recv_tc_count: 累计收到含 tool_calls 的 chunk 数。
        in_think_tag: 流式 ``<think/>`` 标签状态机当前是否处于开标签内。
        on_chunk: 流式 chunk 回调（只读配置）。
        max_thinking_chars: 思考内容截断阈值（只读配置）。
    """

    # 累积输出
    result_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls_map: dict[int, dict[str, Any]] = field(default_factory=dict)
    stream_usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
    finish_reason: str | None = None
    # 计时 / 诊断
    stream_start: float = 0.0
    last_chunk_monotonic: float = 0.0
    chunks_received: int = 0
    recv_seq: int = 0
    recv_tc_count: int = 0
    # <think/> 标签状态机
    in_think_tag: bool = False
    # 只读配置
    on_chunk: Callable[[dict[str, Any]], Any] | None = None
    max_thinking_chars: int = 180000


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
        # 提供者适配（可选插件，按模型名分发）：MiniMax 消息角色安全修正 +
        # openai/ 前缀中转端点的 reasoning_effort/thinking 透传（extra_body）。
        # 与拆分前的内联实现等价，未命中任何规则时行为不变（内置 LiteLLM 直调）。
        messages = apply_pre_send(model, messages, kwargs)

        # provider 适配：按 provider 规则裁剪/转换消息（如 DeepSeek 采样保留 rc）
        # 透传 **kwargs（即 default_params），adapter 按需读取自身配置
        from provider_adapters import get_provider_adapter  # noqa: PLC0415

        adapter = get_provider_adapter(model)
        messages = adapter.adapt_messages_before_send(messages, **kwargs)

        # 弹出 adapter 专属参数（不发给 litellm / API）
        kwargs.pop("reasoning_retention", None)

        # Prompt 审计落盘（默认关，经基础脱敏）：记录真正发往远端 API 的请求体。
        # 放在 provider 适配 + extra_body 处理之后，是 litellm 调用前的最终收口点。
        _log_prompt_body(model, messages, tools, **kwargs)

        if stream:
            return await self._call_streaming(model, messages, tools=tools, on_chunk=on_chunk, **kwargs)
        return await self._call_non_streaming(model, messages, tools=tools, **kwargs)

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
                type(self).__name__,
                model,
                type(exc).__name__,
                exc,
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
        # 流式专属参数对非流式无意义，pop 出来不传给 litellm（与流式路径对齐）。
        # inter_chunk_timeout 是 plugin 传入的 call_timeout，复用为非流式整体超时。
        call_timeout = float(kwargs.pop("inter_chunk_timeout", 300))
        kwargs.pop("first_chunk_timeout", None)
        kwargs.pop("max_thinking_chars", None)

        # 非流式路径必须显式传 float 类型 timeout：litellm 的 Router 默认（yaml
        # call_timeout，可能是 int）或自身默认 int，传给 zai 会触发
        # "Timeout needs to be a float"。显式设 float，与流式路径（3600.0）对齐。
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": call_timeout,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # drop_params 与流式路径对齐：openai provider 不接受 thinking /
        # reasoning_effort 等 deepseek/anthropic 专有参数（自定义中转端点经
        # type=openai 接入时常见），不丢会抛 UnsupportedParamsError。
        # ★ 非流式同样包 _await_with_escape：litellm.acompletion 在内部建连
        # 阶段同样可能吞掉取消挂死（与流式首 chunk 同根因），直接 await 会
        # 让引擎永久卡死。到点抛 TimeoutError 透传，由调用方错误链处理。
        response = await _await_with_escape(
            self._do_completion(**call_kwargs, drop_params=True),
            call_timeout,
            what=f"non-streaming completion model={model}",
        )

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
                    type(self).__name__,
                    len(result_text),
                )

        # 兜底：当 reasoning_content 为空时，手动从 content 中提取 <think/> 标签
        # （DeepSeek/o1 类推理内容，分发到 llm_provider_deepseek 插件）。
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[%s] 从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    type(self).__name__,
                    len(thinking_text),
                    len(result_text or ""),
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
            finish_reason=getattr(choice, "finish_reason", None),
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

        编排：构造参数 → 建连读首 chunk（首字节超时统一覆盖建连→响应头→首字节）
        → 消费流（inter-chunk 静默超时 + 心跳/硬超时兜底 + usage/chunk 处理）
        → 收尾汇总返回 ``LLMResponse``。各阶段委托给私有辅助方法，跨方法状态经
        ``_StreamState`` 传递，行为与历史单体实现完全等价。

        流式超时语义：
        - ``first_chunk_timeout``：首个 chunk 检测连接是否建立（建连/响应头/首字节全程）。
        - ``inter_chunk_timeout``：连续 N 秒收不到任何 chunk 即判定上游/传输静默，
          抛 ``litellm.Timeout`` 中断死等；每个 chunk 到达即重置计时器，活跃推理永不触发。
        """
        call_kwargs, first_chunk_timeout, inter_chunk_timeout, max_thinking_chars = (
            self._build_streaming_call_kwargs(model, messages, tools=tools, kwargs=kwargs)
        )
        response, first_chunk = await self._establish_first_chunk(
            call_kwargs, model, first_chunk_timeout
        )
        state = _StreamState(on_chunk=on_chunk, max_thinking_chars=max_thinking_chars)
        # inter-chunk 静默追踪起点：心跳据此量化"距上个 chunk 多久"。
        state.stream_start = _time.monotonic()
        state.last_chunk_monotonic = state.stream_start
        await self._consume_stream(response, first_chunk, state, model, inter_chunk_timeout)
        return self._build_streaming_response(state)

    def _build_streaming_call_kwargs(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], float, float, int]:
        """构造流式调用参数并解析超时配置。

        与原内联逻辑严格等价：``first_chunk_timeout`` / ``inter_chunk_timeout`` 必须在
        构造 ``call_kwargs`` 之前 pop（否则随 ``**kwargs`` 塞进 litellm 请求参数，litellm
        不识别）；``max_thinking_chars`` 在构造之后 pop（已随 ``**kwargs`` 进入 call_kwargs，
        由 ``_do_completion(..., drop_params=True)`` 丢弃，与原实现一致）。

        Returns:
            ``(call_kwargs, first_chunk_timeout, inter_chunk_timeout, max_thinking_chars)``。
        """
        # 必须在构造 call_kwargs 之前 pop，否则会被 **kwargs 塞进 litellm 请求参数。
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", 180))
        # inter-chunk 静默超时：生产由插件传入 stream_idle_timeout 覆盖；此处默认 600s 兜底。
        inter_chunk_timeout = float(kwargs.pop("inter_chunk_timeout", 600))

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # timeout 传 first_chunk_timeout：KeyPool 路径在 _direct_call_with_slot 会覆盖为
        # first_chunk_timeout 本身——httpx 层超时在线程池线程内生效，事件循环冻结也能
        # 到点抛异常（生产 17:05:34 卡死 36 分钟的根因是 3600s HTTP 超时太长 + asyncio
        # 层超时随事件循环冻结失效）。
        call_kwargs["timeout"] = first_chunk_timeout
        # 与原实现一致：max_thinking_chars 在 call_kwargs 构造后 pop（已随 **kwargs 进入
        # call_kwargs，由 drop_params=True 丢弃），仅本地用于思考截断判断。
        max_thinking_chars = int(kwargs.pop("max_thinking_chars", 180000))
        return call_kwargs, first_chunk_timeout, inter_chunk_timeout, max_thinking_chars

    async def _open_and_first_chunk(
        self,
        call_kwargs: dict[str, Any],
        model: str,
    ) -> tuple[Any, Any]:
        """建连并读取首个 chunk，供外层 wait_for 统一限时。

        首个 chunk 读取若抛异常（含 wait_for 超时注入的 CancelledError），必须关闭
        stream——既为释放 HTTP 连接，也为触发 ``_bind_release_to_stream`` 绑定的
        ``slot.release()``，避免并发许可泄漏（建连超时是高频场景）。

        Returns:
            ``(resp, first_chunk)``。
        """
        _t0 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: 进入，准备调 _do_completion model=%s t0=%.3f",
            type(self).__name__, model, _t0,
        )
        resp = await self._do_completion(**call_kwargs, drop_params=True)
        _t1 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: _do_completion 返回(%.3fs)，准备读首 chunk model=%s",
            type(self).__name__, _t1 - _t0, model,
        )
        try:
            first = await resp.__aiter__().__anext__()
        except BaseException as _first_exc:
            _t2 = _time.monotonic()
            logger.warning(
                "[%s] _open_and_first_chunk: 首chunk异常(%.3fs后) model=%s exc=%s",
                type(self).__name__, _t2 - _t1, model, type(_first_exc).__name__,
            )
            # 超时/异常/取消：关闭流，触发绑定的 release。aclose 自身的任何异常（含
            # CancelledError）都不应掩盖/替换原始异常，故全量抑制。
            # ★ 不能裸 await aclose()：半死 SSL socket 会让 aclose 永久阻塞，把原始异常
            # （超时/取消）吞在 await 里，外层 wait_for 等不到协程退出就永远不返回 → 引擎
            # 死锁。用 _await_with_escape 限时：正常 aclose 毫秒级完成；半死 socket 到点即
            # 放弃，原始异常照常 raise 透传，残留协程后台回收。
            aclose = getattr(resp, "aclose", None)
            if aclose is not None:
                try:
                    await _await_with_escape(
                        aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="first-chunk aclose",
                    )
                except BaseException:
                    pass
            raise
        _t3 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: 首chunk到达(%.3fs后) model=%s",
            type(self).__name__, _t3 - _t1, model,
        )
        return resp, first

    async def _establish_first_chunk(
        self,
        call_kwargs: dict[str, Any],
        model: str,
        first_chunk_timeout: float,
    ) -> tuple[Any, Any]:
        """首字节超时统一覆盖"建连→等响应头→首字节"全过程。

        把 ``first_chunk_timeout`` 的 wait_for 同时包住 ``_do_completion`` 和首 chunk 读取。
        上游"半死连接"（TCP 建连成功、请求已发出，但上游既不回数据也不断开）会让
        ``_do_completion`` 卡在建连/等响应头阶段——若 wait_for 仅包首个 ``__anext__()``
        则因 ``_do_completion`` 尚未返回而无法启动，请求会静默挂死直到 httpx timeout。

        空流（首字节即 EOF）/ 超时 统一转 ``litellm.Timeout``，按首 token 失败处理。

        Returns:
            ``(response, first_chunk)``。
        """
        try:
            response, first_chunk = await _await_with_escape(
                self._open_and_first_chunk(call_kwargs, model),
                first_chunk_timeout,
                what=f"first chunk (incl. connect) model={model}",
            )
        except StopAsyncIteration:
            # 空流：建连成功但首字节即 EOF（零 chunk），按首 token 失败处理。
            # resp 已在 _open_and_first_chunk 内部 aclose，此处无需再关。
            logger.warning(
                "[%s] STREAM EMPTY: 首字节即空流 (建连成功但零 chunk) model=%s，按首 token 失败处理",
                type(self).__name__,
                model,
            )
            raise litellm.Timeout(  # noqa: B904
                message=("Stream first chunk empty: server returned 200 but zero chunks (premature EOF)"),
                model=model,
                llm_provider="zai",
            )
        except asyncio.TimeoutError:
            logger.error(
                "[%s] STREAM TIMEOUT: first chunk 超时 (%.0fs) 含建连阶段 model=%s",
                type(self).__name__,
                first_chunk_timeout,
                model,
            )
            raise litellm.Timeout(  # noqa: B904
                message=(f"Stream first chunk timeout (incl. connect): no response for {first_chunk_timeout:.0f}s"),
                model=model,
                llm_provider="zai",
            )
        return response, first_chunk

    def _handle_delta_content(
        self,
        content: str,
        state: _StreamState,
    ) -> bool:
        """处理 ``delta.content``：流式 ``<think/>`` 标签状态机 + 正文路由。

        返回 True 表示应中断主循环（思考截断 / on_chunk stop 信号）。状态机通过
        ``state.in_think_tag`` 跟踪 ``<think`` / ``</think`` 开闭，确保跨 chunk 切分的
        标签也能正确把思考内容路由到 thinking 通道、正文路由到 text 通道。
        MiniMax 等模型的思考内容以 ``<think/>`` 包裹在 ``delta.content`` 中返回（而非
        ``delta.reasoning_content``）。与原 ``_process_chunk`` 内联段严格等价。
        """
        on_chunk = state.on_chunk
        if state.in_think_tag:
            # 标签内：检查闭合标签
            if "</think" in content:
                close_idx = content.index("</think")
                _think_part = content[:close_idx]
                if _think_part:
                    state.thinking_parts.append(_think_part)
                    if on_chunk:
                        on_chunk({"type": "thinking", "content": _think_part})
                _after_close = content[close_idx:]
                _gt = _after_close.find(">")
                _rest = _after_close[_gt + 1 :] if _gt >= 0 else ""
                state.in_think_tag = False
                if _rest.strip():
                    state.result_parts.append(_rest)
                    if on_chunk:
                        signal = on_chunk({"type": "text", "content": _rest})
                        if signal == "stop":
                            state.stream_repetition = True
                            return True
            else:
                state.thinking_parts.append(content)
                _stream_logger.debug(
                    "[STREAM][THINKING] #%d +%d chars",
                    len(state.thinking_parts),
                    len(content),
                )
                if on_chunk:
                    on_chunk({"type": "thinking", "content": content})
                thinking_len = sum(len(p) for p in state.thinking_parts)
                if state.max_thinking_chars > 0 and thinking_len > state.max_thinking_chars:
                    logger.warning(
                        "[%s] 思考内容过长 (%d>%d chars)，截断",
                        type(self).__name__,
                        thinking_len,
                        state.max_thinking_chars,
                    )
                    state.thinking_truncated = True
                    return True
        # 标签外：检查开标签
        elif "<think" in content:
            _open_idx = content.index("<think")
            _before = content[:_open_idx]
            if _before:
                state.result_parts.append(_before)
                if on_chunk:
                    on_chunk({"type": "text", "content": _before})
            _after_open = content[_open_idx:]
            _gt = _after_open.find(">")
            _inner = _after_open[_gt + 1 :] if _gt >= 0 else ""
            state.in_think_tag = True
            if "</think" in _inner:
                _ci = _inner.index("</think")
                _tp = _inner[:_ci]
                if _tp:
                    state.thinking_parts.append(_tp)
                    if on_chunk:
                        on_chunk({"type": "thinking", "content": _tp})
                _ac = _inner[_ci:]
                _g2 = _ac.find(">")
                _rs = _ac[_g2 + 1 :] if _g2 >= 0 else ""
                state.in_think_tag = False
                if _rs.strip():
                    state.result_parts.append(_rs)
                    if on_chunk:
                        signal = on_chunk({"type": "text", "content": _rs})
                        if signal == "stop":
                            state.stream_repetition = True
                            return True
            elif _inner:
                state.thinking_parts.append(_inner)
                _stream_logger.debug(
                    "[STREAM][THINKING] #%d +%d chars",
                    len(state.thinking_parts),
                    len(_inner),
                )
                if on_chunk:
                    on_chunk({"type": "thinking", "content": _inner})
        else:
            if on_chunk and state.thinking_parts:
                on_chunk({"type": "thinking_end", "content": ""})
            state.result_parts.append(content)
            _stream_logger.debug(
                "[STREAM][TEXT] #%d +%d chars: %s",
                len(state.result_parts),
                len(content),
                repr(content[:80]),
            )
            if on_chunk:
                signal = on_chunk({"type": "text", "content": content})
                if signal == "stop":
                    state.stream_repetition = True
                    logger.warning(
                        "[%s] 收到 stop 信号，截断流式输出",
                        type(self).__name__,
                    )
                    return True
        return False

    async def _process_chunk(self, chunk: Any, state: _StreamState) -> bool:  # noqa: PLR0911,PLR0912,PLR0915
        """处理单个 chunk，返回是否应该 break（中断主循环）。

        含流式诊断日志、usage 核算、``reasoning_content`` / ``<think/>`` / tool_calls 路由。
        与原 ``_call_streaming`` 内联的 ``_process_chunk`` 严格等价，状态经 ``state``
        传递（无闭包 / nonlocal）。
        """
        on_chunk = state.on_chunk
        # 流式诊断：只写文件，不显示在 CLI
        _chunk_idx = len(state.result_parts) + len(state.thinking_parts)
        if _chunk_idx <= 1 or _chunk_idx % 200 == 0:
            _sync_diag_handlers()
            if _diag_logger.handlers:
                _delta = getattr(
                    getattr(chunk, "choices", [None])[0],
                    "delta",
                    None,
                )
                _tc = getattr(_delta, "tool_calls", None)
                _usage = getattr(chunk, "usage", None)
                if _chunk_idx <= 1 or _tc or _usage:
                    _rc = getattr(_delta, "reasoning_content", None)
                    _ct = getattr(_delta, "content", None)
                    _diag_logger.debug(
                        "[%s] chunk #%d: content=%s reasoning=%s tc=%s usage=%s",
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
            state.stream_usage = {
                "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
            }

        if not chunk.choices:
            return False

        delta = chunk.choices[0].delta

        # LiteLLM 统一推理内容映射到 delta.reasoning_content
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            state.thinking_parts.append(reasoning)
            _stream_logger.debug(
                "[STREAM][THINKING] #%d +%d chars",
                len(state.thinking_parts),
                len(reasoning),
            )
            if on_chunk:
                on_chunk({"type": "thinking", "content": reasoning})
            # 思考内容过长 → 截断
            thinking_len = sum(len(p) for p in state.thinking_parts)
            if state.max_thinking_chars > 0 and thinking_len > state.max_thinking_chars:
                logger.warning(
                    "[%s] 思考内容过长(%d>%d chars)，截断",
                    type(self).__name__,
                    thinking_len,
                    state.max_thinking_chars,
                )
                state.thinking_truncated = True
                return True

        # 文本内容：流式 <think/> 状态机处理（MiniMax 等模型）
        if delta.content:
            if self._handle_delta_content(delta.content, state):
                return True

        # 工具调用（流式增量）
        if delta.tool_calls:
            # thinking→tool_calls 过渡：发送 thinking_end 确保思考完整关闭后再输出工具卡片
            if on_chunk and state.thinking_parts:
                on_chunk({"type": "thinking_end", "content": ""})
            for tc in delta.tool_calls:
                idx = tc.index if hasattr(tc, "index") else 0
                if idx not in state.tool_calls_map:
                    state.tool_calls_map[idx] = {
                        "id": (getattr(tc, "id", None) or f"tc_{idx}_{id(state.tool_calls_map)}"),
                        "name": "",
                        "arguments": "",
                    }
                    _stream_logger.debug(
                        "[STREAM][TOOL_CALL] #%d new: id=%s",
                        idx,
                        state.tool_calls_map[idx]["id"],
                    )
                if tc.function:
                    if tc.function.name:
                        state.tool_calls_map[idx]["name"] += tc.function.name
                        _stream_logger.debug(
                            "[STREAM][TOOL_CALL] #%d name=%s",
                            idx,
                            state.tool_calls_map[idx]["name"],
                        )
                    if tc.function.arguments:
                        state.tool_calls_map[idx]["arguments"] += tc.function.arguments
                        _arg_len = len(state.tool_calls_map[idx]["arguments"])
                        _stream_logger.debug(
                            "[STREAM][TOOL_CALL] #%d args +%d → %d chars: %s",
                            idx,
                            len(tc.function.arguments),
                            _arg_len,
                            repr(tc.function.arguments[:100]),
                        )

            if on_chunk:
                on_chunk(
                    {
                        "type": "tool_call",
                        "tool_calls": delta.tool_calls,
                    }
                )
        return False

    async def _consume_stream(
        self,
        response: Any,
        first_chunk: Any,
        state: _StreamState,
        model: str,
        inter_chunk_timeout: float,
    ) -> None:
        """消费流：处理首 chunk → 启动心跳/硬超时 → 按 inter_chunk_timeout 循环消费。

        首 chunk 已在 ``_establish_first_chunk`` 内读取（含建连阶段超时保护），此处直接
        处理它，随后启动心跳探针（asyncio，证明接收协程存活 + 量化静默时长）与独立线程
        硬超时（loop 冻住也能生效的兜底），再逐次超时消费后续 chunk。每个 chunk 到达即
        重置计时器（活跃推理不误触发，仅真正静默累计满 timeout 才掐断）。

        finally 收尾：取消心跳、disarm 硬超时、aclose 底层流（带超时逃逸，防半死 socket
        卡死引擎）。与原 ``_call_streaming`` 主循环 + finally 严格等价。
        """
        # litellm CustomStreamWrapper 的底层流对象（openai/zai 路径即 httpx.Response）。
        # 心跳日志读其 is_closed 作为半死 TCP 的廉价（不可靠但便宜）附加信号。
        _completion_stream: Any = getattr(response, "completion_stream", None)
        # 心跳探针任务句柄（首 chunk 后启动，finally 中取消）。
        _heartbeat_task: asyncio.Task[None] | None = None
        # 独立线程硬超时句柄（首 chunk 后 arm，finally 中 disarm）。asyncio 心跳 /
        # inter_chunk wait_for 在 loop 被 socket 阻塞冻住时全部失效（实测：僵死管道零
        # HEARTBEAT 日志）。watchdog 用 threading 线程倒计时，到点强制 stream.aclose()
        # 打破死锁，是 loop 冻住也能生效的兜底。
        _hard_timeout: StreamHardTimeout | None = None

        aiter = response.__aiter__()
        try:
            # 首个 chunk 已在建连阶段读取，此处直接处理它。
            chunk = first_chunk
            await self._process_chunk(chunk, state)
            state.last_chunk_monotonic = _time.monotonic()
            state.chunks_received += 1
            # 启动心跳探针：流静默时持续打 idle 时长 + stream_closed，证明接收协程存活
            # （排除接收端死锁），并量化上游/传输静默时长。
            _heartbeat_task = asyncio.create_task(
                self._stream_heartbeat(
                    model,
                    inter_chunk_timeout,
                    lambda: _time.monotonic() - state.last_chunk_monotonic,
                    lambda: state.chunks_received,
                    _completion_stream,
                )
            )
            # 硬超时语义为"chunk 间隔超时"：每收到一个 chunk 调 reset() 重新计时，
            # 避免误杀总时长长但 chunk 间隔始终健康的流。
            _hard_timeout = StreamHardTimeout(
                response,
                asyncio.get_running_loop(),
                inter_chunk_timeout,
            )
            _hard_timeout.arm()
            # 接收端点诊断（首个 chunk）
            state.recv_seq += 1
            try:
                _rc0 = chunk.choices[0] if chunk.choices else None
                if _rc0 is not None:
                    _d0 = getattr(_rc0, "delta", None)
                    _fr0 = getattr(_rc0, "finish_reason", None)
                    _tc0 = getattr(_d0, "tool_calls", None) if _d0 else None
                    if _tc0:
                        state.recv_tc_count += 1
                        _tc_summary0 = []
                        for _tci in _tc0:
                            _fn0 = getattr(_tci, "function", None)
                            _tc_name0 = getattr(_fn0, "name", "?") if _fn0 else "?"
                            _tc_args0 = getattr(_fn0, "arguments", "") if _fn0 else ""
                            _tc_summary0.append(f"{_tc_name0}(args={len(_tc_args0)}c)")
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d tool_calls 到达(首chunk, %d个): %s",
                            state.recv_seq,
                            len(_tc0),
                            ", ".join(_tc_summary0),
                        )
                    if _fr0:
                        state.finish_reason = _fr0
                        _stream_logger.debug(
                            "[STREAM][RECV] #%d finish=%s (首chunk, 累计tc=%d)",
                            state.recv_seq,
                            _fr0,
                            state.recv_tc_count,
                        )
            except Exception:
                pass

            # 后续 chunk：逐次超时，每个 chunk 到达即重置计时器。
            # 活跃推理 chunk 间隔远小于 timeout 故不误触发；仅真正静默（死连接）累计满 timeout。
            # 用 _await_with_escape：即使底层 __anext__ 吞掉取消挂死（半死连接），也能到点
            # 抛错透传，而不是被 asyncio.wait_for「等协程退出」卡死。
            while True:
                try:
                    chunk = await _await_with_escape(
                        aiter.__anext__(),
                        inter_chunk_timeout,
                        what=f"inter-chunk model={model}",
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    _idle = _time.monotonic() - state.last_chunk_monotonic
                    logger.warning(
                        "[%s] STREAM TIMEOUT: inter-chunk 静默超时 (%.0fs) 距上个 chunk #%d 已静默 %.0fs model=%s",
                        type(self).__name__,
                        inter_chunk_timeout,
                        state.chunks_received,
                        _idle,
                        model,
                    )
                    raise litellm.Timeout(  # noqa: B904
                        message=(
                            "Stream inter-chunk timeout:"
                            f" no data for {_idle:.0f}s"
                            f" (last chunk #{state.chunks_received}, timeout={inter_chunk_timeout:.0f}s)"
                        ),
                        model=model,
                        llm_provider="zai",
                    )
                state.last_chunk_monotonic = _time.monotonic()
                state.chunks_received += 1
                # chunk 健康到达：重置硬超时倒计时（chunk 间隔语义，避免误杀长流）
                if _hard_timeout is not None:
                    _hard_timeout.reset()
                if await self._process_chunk(chunk, state):
                    break
                # ── 接收端点诊断：每个 chunk 检查 delta.tool_calls 是否到达 ──
                state.recv_seq += 1
                try:
                    _rc = chunk.choices[0] if chunk.choices else None
                    if _rc is not None:
                        _d = getattr(_rc, "delta", None)
                        _fr = getattr(_rc, "finish_reason", None)
                        _tc = getattr(_d, "tool_calls", None) if _d else None
                        if _tc:
                            state.recv_tc_count += 1
                            # 打印完整 tool_call 内容（name + arguments 长度 + 预览）
                            _tc_summary = []
                            for _tci in _tc:
                                _fn = getattr(_tci, "function", None)
                                _tc_name = getattr(_fn, "name", "?") if _fn else "?"
                                _tc_args = getattr(_fn, "arguments", "") if _fn else ""
                                _tc_summary.append(f"{_tc_name}(args={len(_tc_args)}c)")
                            _stream_logger.debug(
                                "[STREAM][RECV] #%d tool_calls 到达(%d个): %s",
                                state.recv_seq,
                                len(_tc),
                                ", ".join(_tc_summary),
                            )
                            state.finish_reason = _fr
                            _stream_logger.debug(
                                "[STREAM][RECV] #%d finish=%s (累计tc=%d)",
                                state.recv_seq,
                                _fr,
                                state.recv_tc_count,
                            )
                except Exception:
                    pass
        finally:
            # 取消心跳探针任务（避免任务泄漏：超时/异常/正常结束都要清理）
            if _heartbeat_task is not None and not _heartbeat_task.done():
                _heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await _heartbeat_task
            # 取消独立线程硬超时（正常结束时不触发强制关闭，幂等）
            if _hard_timeout is not None:
                _hard_timeout.disarm()
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接。
            # 超时兜底：Windows 半死 SSL socket 会让 httpx aclose 永久阻塞，导致本 finally
            # 不返回 → _run_loop 永久卡死。超时后放弃关闭，协程得以返回，残留 socket
            # （CLOSE_WAIT）交由 GC/OS 回收。KeyPoolAdapter 路径下 aclose 已被
            # _aclose_with_release 包过一层 wait_for，这里再包一层无害；LiteLLMAdapter
            # 路径下 aclose 是原始的，本层是其唯一保护。★ 用 _await_with_escape：即使
            # aclose 吞掉取消挂死，也能到点返回（此处不抛错，仅记录日志），避免 finally
            # 卡死引擎。
            if hasattr(response, "aclose"):
                try:
                    await _await_with_escape(
                        response.aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="response.aclose",
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] response.aclose finally 超时 %.0fs（半死 socket 放弃关闭），"
                        "残留连接交 GC 回收",
                        type(self).__name__,
                        _ACLOSE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.debug("[%s] response.aclose finally 异常（已忽略）", type(self).__name__)

    def _build_streaming_response(self, state: _StreamState) -> LLMResponse:
        """汇总流式累积状态，记录速度/接收统计，构造并返回 ``LLMResponse``。

        与原 ``_call_streaming`` 收尾段（文本拼接 + usage/速度统计 + 接收端点汇总 +
        ``LLMResponse`` 构造）严格等价。
        """
        result_text = "".join(state.result_parts) if state.result_parts else None
        thinking_text = "".join(state.thinking_parts) if state.thinking_parts else None
        tool_calls = self._normalize_tool_calls(state.tool_calls_map)

        # 流式接收完成：记录速度统计
        _stream_elapsed = _time.monotonic() - state.stream_start
        _comp_tokens = (state.stream_usage or {}).get("completion_tokens", 0)
        _speed = (_comp_tokens / _stream_elapsed) if _stream_elapsed > 0 and _comp_tokens else 0
        _stream_logger.debug(
            "[STREAM][DONE] finish=%s text=%d chars thinking=%d chars "
            "chunks=%d tool_calls=%d "
            "tokens=%d elapsed=%.2fs speed=%.1f tok/s",
            state.finish_reason,
            len(result_text or ""),
            len(thinking_text or ""),
            len(state.result_parts) + len(state.thinking_parts),
            len(tool_calls),
            _comp_tokens,
            _stream_elapsed,
            _speed,
        )
        # 接收端点汇总：API 端实际送达的 tool_calls chunk 数 vs 最终解析数
        _stream_logger.debug(
            "[STREAM][STATS] recv_chunks=%d recv_tc=%d parsed_tc=%d",
            state.recv_seq,
            state.recv_tc_count,
            len(tool_calls),
        )

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=state.stream_usage,
            stream_repetition=state.stream_repetition,
            thinking_truncated=state.thinking_truncated,
            finish_reason=state.finish_reason,
        )


    async def _stream_heartbeat(
        self,
        model: str,
        inter_chunk_timeout: float,
        idle_getter: Callable[[], float],
        chunks_getter: Callable[[], int],
        completion_stream: Any,
    ) -> None:
        """流式心跳探针：周期性打 idle 时长 + stream_closed 信号。

        诊断目标（区分"上游/API 端不发"vs"我们接收端卡死"）：
          - 心跳持续输出 → 接收协程存活，非接收端死锁；
          - idle 时长持续增长 → 上游/传输静默（接收端在等，没人发）；
          - idle 在心跳间隔(30s)附近震荡 → 正常活跃流。

        idle 接近 timeout/2 时升级为 WARNING，使静默即将触发超时时醒目可见。
        stream_closed 取底层 httpx Response.is_closed（对静默半死 TCP 仍可能为
        False，仅作廉价附加信号，不可靠不独断）。

        沿用 process_manager._watchdog_loop 的范式：CancelledError 单独捕获并退出，
        其他异常吞掉保持循环存活。由 _call_streaming 的 finally 负责取消。

        Args:
            model: 模型标识（日志用）
            inter_chunk_timeout: inter-chunk 静默超时阈值（用于决定日志级别）
            idle_getter: 返回距上个 chunk 的秒数（闭包读 _last_chunk_monotonic）
            chunks_getter: 返回累计收到的 chunk 数（闭包读 _chunks_received）
            completion_stream: litellm 底层流对象（读 is_closed）
        """
        half = inter_chunk_timeout / 2
        try:
            while True:
                await asyncio.sleep(30.0)
                idle = idle_getter()
                received = chunks_getter()
                closed = getattr(completion_stream, "is_closed", None) if completion_stream is not None else None
                _stream_logger.log(
                    logging.WARNING if idle >= half else logging.DEBUG,
                    "[STREAM][HEARTBEAT] idle=%.0fs since chunk #%d total=%d stream_closed=%s model=%s",
                    idle,
                    received,
                    received,
                    closed,
                    model,
                )
        except asyncio.CancelledError:
            pass

    def _parse_tool_calls(self, raw_tool_calls: Any) -> list[dict[str, Any]]:
        """解析非流式响应中的 tool_calls。"""
        if not raw_tool_calls:
            return []

        parsed: list[dict[str, Any]] = []
        for tc in raw_tool_calls:
            parsed.append(
                {
                    "id": getattr(tc, "id", None) or f"call_{len(parsed)}",
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                }
            )
        return parsed

    def _normalize_tool_calls(self, tool_calls_map: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        """将流式收集的 tool_calls 映射归一化。"""
        if not tool_calls_map:
            return []

        result: list[dict[str, Any]] = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            result.append(
                {
                    "id": tc.get("id") or f"call_{idx}",
                    "name": tc["name"],
                    "arguments": tc["arguments"],
                }
            )
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
