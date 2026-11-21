# @feature: FP-T07 llm api | @ci: python-coverage
"""LLM 适配服务面流式消费机（批 3 复制改造落位）。

复制锚点
- 源文件：``plugins/shared/llm/adapter.py``（``_BaseLLMAdapter`` 消费路径）
- 源 commit 基线：``c0fe6922fc8cf68cc0c46b6ff1e11c0db021056a``（批 2 收口基线）
- 复制面（逐行同源）：``_ACLOSE_TIMEOUT_SECONDS`` / 后台任务登记 /
  ``StreamCancelledError`` / ``_close_stream_quietly`` /
  ``_prefetch_first_chunk`` / ``_reraise_if_outer_cancel_pending`` /
  ``_await_with_escape`` / ``_normalize_timeout_keys`` / ``_StreamState`` /
  ``completion`` / ``health_check`` / ``_call_non_streaming`` /
  ``_call_streaming`` / ``_establish_first_chunk`` / ``_open_and_first_chunk`` /
  ``_consume_stream`` / ``_process_chunk`` / 思考与工具调用增量合并 /
  ``_stream_heartbeat`` / 响应解析（``_parse_tool_calls`` 等）。
- 改造清单（仅下列差异）：
  1. 类名 ``_BaseLLMAdapter`` → ``WireStreamAdapter``（适配服务面基类，wire 由
     子类 ``_do_completion`` 注入）；``wire_client`` → ``llm_wire``；
     ``provider_adapters`` → ``llm_provider_adapters``；
  2. **诊断日志面不随复制**：``_log_chunk_flow_diag``（chunk 流动日志）与
     ``_sync_diag_handlers`` 不复制；``_diag_recv_chunk`` 精简为
     「接收端点观察」——只保留 finish_reason/tool_calls 计数语义（它是功能面
     而非纯日志），日志细节归网关；
  3. ``_build_streaming_response`` 去掉速度统计/统计日志（诊断面），
     拼接/归一/finish_reason 收敛语义不变；
  4. 非流式路径显式携带 ``stream: False``（源件该键缺席使 wire 分支默认流式，
     非流式路径为死路——复制件显式化）；
  5. 输出完即清（内存定案 §8 条款 2）：``_StreamState.release_buffers`` 在
     ``_call_streaming`` 收尾（成功/异常/取消同规格）释放全部缓冲与回调引用；
  6. 生命周期（§6.2 三线）：实例不挂 per-request 状态；取消传播走进程内
     ``cancel_event``（跨进程轮询通道留网关）。

职责：归一 chunk 消费状态机 + 超时/取消/半死连接防护 + 聚合响应构建。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import llm_wire
from adapter import LLMResponse
from llm_minimax import _extract_thinking_from_content, parse_minimax_native_tool_calls

from agentos_plugin_sdk.stream_watchdog import StreamHardTimeout

logger = logging.getLogger(__name__)


# aclose 超时上限（流式连接关闭）：正常关闭一个 HTTP 连接仅需毫秒级，超过该阈值
# 说明底层 socket（常为 Windows ProactorEventLoop 上的 SSL 流）已半死——服务端发了
# FIN 但本地 SSL shutdown 握手等不到响应，httpx/httpcore 的 aclose 会永久阻塞。
# 此时放弃优雅关闭，让协程返回，残留 socket（CLOSE_WAIT）交由 GC/OS 回收。
# 选 10s 是远大于健康关闭耗时、又远小于让管道僵死的可忍受时长。
_ACLOSE_TIMEOUT_SECONDS: float = 10.0

# 后台残留任务登记表：_await_with_escape 超时放弃等待后，被取消的协程若吞掉
# CancelledError 继续挂起（上游半死连接场景），task 会留在后台运行。
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


# 首块预读哨兵：流对象未携带预读首块（_prefetched_first_chunk 缺席）时的标记。
_NO_PREFETCH: Any = object()

class StreamCancelledError(Exception):
    """流式调用被调用方停止（cancel_event 置位）——内部控制流异常，不出服务边界。

    抛出点有两类，最终都交 server 层既有 interrupted 返回链收尾（半截快照
    + ``finish_reason=interrupted``）：
    - server ``on_chunk`` 检查点：chunk 到达时事件已置位（流活跃路径）；
    - adapter ``_await_with_escape`` abort 通道：等待 ``__anext__``/首块预取
      期间事件置位（流静默路径——上游不吐 chunk 时取消不再干等到超时）。
    """


async def _close_stream_quietly(stream: Any) -> None:
    """限时关闭流（失败不掩盖正在传播的业务异常；取消语义保留穿透）。"""
    aclose = getattr(stream, "aclose", None)
    if aclose is None:
        return
    try:
        await _await_with_escape(aclose(), _ACLOSE_TIMEOUT_SECONDS, what="prefetch aclose")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 —— 关闭失败不掩盖主异常
        pass


async def _prefetch_first_chunk(stream: Any, timeout: float, model: str) -> Any:
    """首块预读（L5 超时后处置归类律）：首块超时必须在重试归属窗口内到点。

    首块读取若留在逐 key 重试循环之外（流对象返回后的消费期），冷却/换 key/
    兜底链对超时类全部接不到（装机 2026-11-05：19 次首块超时全部裸穿 MCP
    ``tools/call``、``fallback_notice`` 全天 0 次）。超时/空流统一转
    WireTimeoutError（分类面单点）；读取失败关闭底层流（连接回收；绑定
    release 由循环 finally 承接）。
    """
    try:
        return await _await_with_escape(
            stream.__aiter__().__anext__(),
            timeout,
            what=f"first chunk (incl. connect) model={model}",
        )
    except StopAsyncIteration:
        exc: BaseException = llm_wire.WireTimeoutError(
            "Stream first chunk empty: server returned 200 but zero chunks (premature EOF)"
        )
    except TimeoutError:
        exc = llm_wire.WireTimeoutError(
            f"Stream first chunk timeout (incl. connect): no response for {timeout:.0f}s"
        )
    except BaseException:
        await _close_stream_quietly(stream)
        raise
    await _close_stream_quietly(stream)
    raise exc


def _reraise_if_outer_cancel_pending() -> None:
    """外层任务级取消在飞 → 重抛 CancelledError（不得被取消收尾吞掉）。

    server 契约「任务级取消必须原样传播」：本函数只统计**当前任务**被外部
    cancel 的次数（Task.cancelling），自身对子任务的 task.cancel() 不计入。
    """
    current = asyncio.current_task()
    if current is not None and current.cancelling() > 0:
        raise asyncio.CancelledError


async def _await_with_escape(
    coro: Any,
    timeout: float,
    *,
    what: str,
    abort_event: asyncio.Event | None = None,
) -> Any:
    """带超时等待协程，超时即抛错；``abort_event`` 置位即中断（流式取消通道）。

    asyncio.wait 的 timeout 依赖事件循环调度——若协程内部同步阻塞冻住事件循环，
    timeout 回调永远不执行。加独立线程诊断：到点如果 task 还没完成，独立线程
    直接打日志（不依赖事件循环），证明「超时确实该触发但事件循环冻住了」。

    abort_event（调用方停止信号）与本体竞速（FIRST_COMPLETED）：信号胜出时
    cancel 本体并等其收尾（关流/释放许可由各层 finally 完成），抛
    StreamCancelledError 交上层 interrupted 返回链——流式取消从「干等到下一个
    chunk/超时（缺省 600s）」降为信号传播本身。本体先完成（含与信号同时）按
    完成处理：chunk 流动场景的取消由 on_chunk 检查点接管，不在此抢终局。
    超时语义不变（inter_chunk_timeout 到点 = 原有超时错误路径）。
    """
    import threading  # noqa: PLC0415
    task = asyncio.ensure_future(coro)
    _track_background_task(task)
    abort_task: asyncio.Task[bool] | None = None
    if abort_event is not None:
        abort_task = asyncio.ensure_future(abort_event.wait())

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

    wait_set: set[asyncio.Future[Any]] = {task}
    if abort_task is not None:
        wait_set.add(abort_task)
    done, _pending = await asyncio.wait(
        wait_set, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )
    diag_timer.cancel()
    if not done:
        logger.error(
            "[_await_with_escape] 超时！cancel task | what=%s timeout=%.0fs",
            what, timeout,
        )
        task.cancel()
        if abort_task is not None:
            abort_task.cancel()
        raise TimeoutError(f"{what} 超时 {timeout:.0f}s")
    if abort_task is not None and abort_task in done and task not in done:
        # 停止信号胜出：cancel 本体并限时等其收尾（finally 关流/释放许可照常
        # 执行），随后抛取消异常。外层任务级取消在飞时不吞（原样传播）。
        logger.info(
            "[_await_with_escape] 停止信号置位，中断等待 | what=%s",
            what,
        )
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        _reraise_if_outer_cancel_pending()
        raise StreamCancelledError(f"cancelled during {what}")
    # 本体完成（先于/同时于取消信号）：回收取消等待任务防悬挂
    if abort_task is not None and not abort_task.done():
        abort_task.cancel()
    return task.result()

def _normalize_timeout_keys(kwargs: dict[str, Any]) -> None:
    """llm.yaml 配置键 → adapter 内部超时键归一（原地修改）。

    llm_core 经 llm.complete_stream kwargs 携带配置原生键
    （first_token_timeout/stream_idle_timeout）；直连/测试调用历史用内部键
    （first_chunk_timeout/inter_chunk_timeout）。键名不一致曾使生产配置
    （stream_idle_timeout=180）从未生效——流中途冻结恒等 600s 内部兜底才判错
    （装机 2026-11-04 死窗 10min28s 事故）。两组键都弹出：残留键会随
    **kwargs 外发上游触发参数拒绝。

    优先级：内部键 > 配置键 > 调用点默认。
    """
    if "first_chunk_timeout" in kwargs:
        kwargs.pop("first_token_timeout", None)
    else:
        first_token = kwargs.pop("first_token_timeout", None)
        if first_token is not None:
            kwargs["first_chunk_timeout"] = first_token
    if "inter_chunk_timeout" in kwargs:
        kwargs.pop("stream_idle_timeout", None)
    else:
        idle = kwargs.pop("stream_idle_timeout", None)
        if idle is not None:
            kwargs["inter_chunk_timeout"] = idle


@dataclass
class _StreamState:
    """单次流式调用的跨方法共享状态。

    ``_call_streaming`` 按职责拆分为多个私有辅助方法后，原闭包/局部可变状态统一由本
    dataclass 持有并在方法间传递（对象属性原地修改），各方法无需 ``nonlocal`` 即可
    读写同一份状态——行为与拆分前的闭包实现完全等价。

    Attributes:
        result_parts: 正文文本片段（按 chunk 顺序累积）。
        thinking_parts: 思考内容片段（reasoning_content / ``<think/>`` 标签内容）。
        tool_calls_map: 流式 tool_calls 增量按 index 合并的映射。
        stream_usage: 最后一次收到的流式 usage（通常在末尾 chunk）。
        stream_repetition: 是否因 ``on_chunk`` 返回 ``"stop"``（重复检测）而截断。
        thinking_truncated: 思考内容是否因超过 ``max_thinking_chars`` 被截断。
        finish_reason: 结束原因观察值（接收端点观察捕获，上层截断判定输入）。
        on_chunk: 流式 chunk 回调（只读配置）。
        max_thinking_chars: 思考内容截断阈值（只读配置）。
        stream_start: 流式消费起始 monotonic 时间（耗时统计用）。
        last_chunk_monotonic: 上个 chunk 到达的 monotonic 时间（心跳量化静默用）。
        chunks_received: 累计收到的 chunk 数（心跳/超时日志用）。
        recv_seq: 接收端点观察序号（chunk 到达次数）。
        recv_tc_count: 累计收到含 tool_calls 的 chunk 数。
        in_think_tag: 流式 ``<think/>`` 标签状态机当前是否处于开标签内。
    """

    result_parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    tool_calls_map: dict[int, dict[str, Any]] = field(default_factory=dict)
    stream_usage: dict[str, Any] | None = None
    stream_repetition: bool = False
    thinking_truncated: bool = False
    finish_reason: str | None = None
    # 只读配置
    on_chunk: Callable[[dict[str, Any]], Any] | None = None
    max_thinking_chars: int = 180000
    # 计时 / 观察计数
    stream_start: float = 0.0
    last_chunk_monotonic: float = 0.0
    chunks_received: int = 0
    recv_seq: int = 0
    recv_tc_count: int = 0
    in_think_tag: bool = False

    def release_buffers(self) -> None:
        """输出完即清（§8 条款 2）：释放响应缓冲与回调引用，跨请求零保留。

        只清本状态对象持有的引用：聚合结果（``LLMResponse`` 的 text/thinking/
        tool_calls/usage）已在 ``_build_streaming_response`` 内成对象，不依赖
        本对象存活。异常/取消路径同样调用（半截快照由消费方累积器独立持有）。
        """
        self.result_parts.clear()
        self.thinking_parts.clear()
        self.tool_calls_map.clear()
        self.stream_usage = None
        self.on_chunk = None


class WireStreamAdapter:
    """适配服务面流式消费基类（复制锚点见文件头）。

    子类只需实现 ``_do_completion()`` 提供实际的 wire 调用入口，
    基类负责流式/非流式调用编排、超时/取消防护与响应解析。
    实例不挂 per-request 状态（生命周期线，§6.2）。
    """

    async def _do_completion(self, **kwargs: Any) -> Any:
        """执行实际的 LLM API 调用，子类必须覆写。"""
        raise NotImplementedError

    @staticmethod
    def _ensure_minimax_role_safety(
        model: str,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """防御性兜底：确保 minimax 模型不会收到非首位 system 消息。

        根因：MiniMax API 仅允许首位消息为 system role。管道中的
        StreamRepetitionGuard、ThinkingTruncationGuard 等会注入 system 消息，
        _normalize_messages_for_provider 的 Phase 1-4 已做转换，但极端边界
        情况可能遗漏。此方法作为最后一道防线，在 adapter 层拦截。

        Args:
            model: 模型标识字符串（如 "minimax/MiniMax-M2.7"）
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
                    i,
                    str(msg.get("content", ""))[:100],
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
        replay_reasoning: bool | str | None = None,
        cancel_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """执行 LLM 调用，支持非流式和流式两种模式。

        ``cancel_event``（流式取消信号）为签名级专用参数：不入 ``**kwargs``
        （避免作为模型参数外发），仅流式路径显式传入 ``_call_streaming``。
        """
        # 防御性兜底 + provider 适配：大历史逐条浅拷贝/角色修复是同步 CPU，
        # 必须出事件循环——2026-11-12 第 39 轮装机取证栈命中
        # `minimax.py:44 adapt_messages_before_send`，单次 >30s 触发宿主
        # watchdog `os._exit(1)`。
        from llm_provider_adapters import get_provider_adapter  # noqa: PLC0415

        adapter = get_provider_adapter(model)
        messages = await asyncio.to_thread(
            self._adapt_messages_before_send_sync,
            adapter,
            model,
            messages,
            replay_reasoning,
            kwargs,
        )

        # 工具面出站观测（BUG-17 主路径断言点）：行缺失 = 本轮请求无 tools，
        # 模型将凭历史降级为正文输出原生工具标记。
        if tools:
            logger.info(
                "[adapter] 工具面出站 tools=%d | model=%s names=%s",
                len(tools),
                model,
                [
                    (t.get("function") or {}).get("name", "?")
                    for t in tools
                    if isinstance(t, dict)
                ][:20],
            )

        # 弹出 adapter 专属参数（不发上游）
        kwargs.pop("reasoning_retention", None)
        # provider 插件信封（网关化第二步）：仅 via=plugin 分支消费；内置路径
        # 到此弹出（残留会随 **kwargs 进上游请求体）
        kwargs.pop("_plugin_call_ctx", None)
        kwargs.pop("_plugin_fallback_depth", None)



        if stream:
            return await self._call_streaming(
                model,
                messages,
                tools=tools,
                on_chunk=on_chunk,
                cancel_event=cancel_event,
                **kwargs,
            )
        return await self._call_non_streaming(model, messages, tools=tools, **kwargs)

    def _adapt_messages_before_send_sync(
        self,
        adapter: Any,
        model: str,
        messages: list[dict[str, Any]],
        replay_reasoning: bool | str | None,
        kwargs: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """同步消息适配体（在线程池执行，禁止 await 后读共享实例字段）。"""
        self._ensure_minimax_role_safety(model, messages)
        return adapter.adapt_messages_before_send(
            messages, replay_reasoning=replay_reasoning, **kwargs
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
        # 流式专属参数对非流式无意义，pop 出来不传上游（与流式路径对齐）。
        # inter_chunk_timeout 是 plugin 传入的 call_timeout，复用为非流式整体超时。
        call_timeout = float(kwargs.pop("inter_chunk_timeout", 300))
        kwargs.pop("first_chunk_timeout", None)
        kwargs.pop("max_thinking_chars", None)

        # 非流式路径必须显式传 float 类型 timeout：上游 SDK 对 int 形态有
        # 拒收史（"Timeout needs to be a float"）。显式设 float，与流式路径对齐。
        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            # 改造（见文件头改造清单 4）：源件该键缺席使 wire 分支默认流式
            # （非流式路径为死路）；复制件显式携带 stream=False 走非流式 send。
            "stream": False,
            "timeout": call_timeout,
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # drop_params 与流式路径对齐：openai provider 不接受 thinking /
        # reasoning_effort 等 deepseek/anthropic 专有参数（自定义中转端点经
        # type=openai 接入时常见），不丢会抛 UnsupportedParamsError。
        # ★ 非流式同样包 _await_with_escape：传输层在内部建连
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
        if not thinking_text and result_text:
            extracted_thinking, cleaned_content = _extract_thinking_from_content(result_text)
            if extracted_thinking:
                thinking_text = extracted_thinking
                result_text = cleaned_content
                logger.info(
                    "[%s] 从 <think/> 标签提取 thinking (thinking=%d, content=%d)",
                    type(self).__name__,
                    len(thinking_text),
                    len(result_text or ""),
                )

        usage: dict[str, Any] | None = None
        if hasattr(response, "usage") and response.usage:
            _prompt_details = getattr(response.usage, "prompt_tokens_details", None)
            usage = {
                "prompt_tokens": getattr(response.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(response.usage, "completion_tokens", 0),
                "total_tokens": getattr(response.usage, "total_tokens", 0),
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
            }

        result = LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=usage,
            finish_reason=getattr(choice, "finish_reason", None),
        )
        self._recover_minimax_text_tool_calls(result, model)
        return result

    async def _call_streaming(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        on_chunk: Callable[[dict[str, Any]], Any] | None = None,
        cancel_event: asyncio.Event | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """流式调用 LLM。

        阶段序列：
        1. 参数准备：剥出流式专属参数，构造 wire 调用参数；
        2. 建连+首 chunk（_establish_first_chunk）：首字节超时统一覆盖
           "建连→等响应头→首字节"全过程；
        3. 消费流（_consume_stream）：inter-chunk 静默超时 + 心跳探针 +
           独立线程硬超时兜底，finally 里限时清理全部资源；
        4. 汇总累积状态构造响应。

        取消感知：cancel_event 置位（调用方停止）在两处即时中断——首块预取
        与逐 chunk 等待均与该信号竞速（_await_with_escape abort 通道），抛
        StreamCancelledError；流活跃时 chunk 到达即经 on_chunk 检查点中断
        （回调自检信号，与本层无关）。

        Args:
            model: provider 前缀形态的模型标识字符串
            messages: 对话消息列表
            tools: 工具 Schema 列表（可选）
            on_chunk: 流式 chunk 回调（可选）；回调返回 "stop" 可截断输出
            cancel_event: 调用方停止信号（可选；None=无取消感知）
            **kwargs: 其余参数透传上游；流式专属键 first_chunk_timeout /
                inter_chunk_timeout / max_thinking_chars 在此剥出，不进入请求参数
        """
        # 流式超时：首个 chunk 检测连接是否建立，后续 chunk 防止连接僵死。
        # 必须在构造 call_kwargs 之前 pop 出来，否则会被 **kwargs 塞进
        # 上游请求参数（wire body 不识别这些 key）。
        # 配置键归一：llm_core 携带 llm.yaml 原生键（stream_idle_timeout），
        # 此处统一为内部键（内部键优先，见助手 docstring）。
        _normalize_timeout_keys(kwargs)
        # inter-chunk 静默超时：连续 N 秒收不到任何 chunk 即判定上游/传输静默，
        # 抛 WireTimeoutError 中断死等。每个 chunk 到达即重置计时器（见主循环），
        # 故活跃推理（reasoning 持续吐 chunk）永不触发，只有真正静默（连接挂起/
        # 上游冻结）才在 N 秒后掐断。生产值经 llm_core 按 llm.yaml defaults/
        # models.<id>.stream_idle_timeout 传入（经 _normalize_timeout_keys 归一）；
        # 此处默认 90s 为直连/测试调用兜底（业界标准量级：nginx proxy_read_timeout
        # 60s / Cloudflare SSE idle 100s；2026-11-06 装机实证 97s 零 delta 滞涩
        # 在旧 600s 兜底下干等不判错）。
        inter_chunk_timeout = float(kwargs.pop("inter_chunk_timeout", 90))
        # 吐字间隔语义统一（2026-11-04 用户裁定）：stream_idle_timeout 的预算
        # 含首 token——首 token 间隔就是第一段吐字间隔，未显式给
        # first_chunk_timeout 时首块预算=吐字间隔预算（不再依赖独立的更短
        # first_token_timeout；显式 first_chunk_timeout 仍可覆盖，供直连/测试）。
        first_chunk_timeout = float(kwargs.pop("first_chunk_timeout", inter_chunk_timeout))

        call_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            **kwargs,
        }
        if tools:
            call_kwargs["tools"] = tools

        # timeout 取 first_chunk_timeout：KeyPool 路径在 _direct_call_with_slot 会
        # 覆盖为 first_chunk_timeout 本身——httpx 层超时在线程池线程内生效，事件
        # 循环冻结也能到点抛异常（httpx 层长超时 + asyncio 层超时随事件循环冻结
        # 失效的组合是引擎级挂死的根因，见 KeyPoolAdapter 同类注释）。
        call_kwargs["timeout"] = first_chunk_timeout

        response, first_chunk = await self._establish_first_chunk(
            model, call_kwargs, first_chunk_timeout, abort_event=cancel_event,
        )

        state = _StreamState(
            on_chunk=on_chunk,
            max_thinking_chars=int(kwargs.pop("max_thinking_chars", 180000)),
            stream_start=_time.monotonic(),
        )
        state.last_chunk_monotonic = state.stream_start
        # 降级信号前置于内容 chunk（观测C）：fallback 接管的流在正文前先发
        # fallback_notice，消费方翻译为用户可见信号。
        notice = getattr(response, "keypool_fallback_notice", None)
        if notice and on_chunk is not None:
            on_chunk({"type": "fallback_notice", **notice})
        try:
            await self._consume_stream(
                response, first_chunk, state, model, inter_chunk_timeout,
                cancel_event=cancel_event,
            )
            result = self._build_streaming_response(state)
        finally:
            # 输出完即清（§8 条款 2）：成功/异常/取消同规格释放全部缓冲与回调引用
            state.release_buffers()
        self._recover_minimax_text_tool_calls(result, model)
        return result

    async def _establish_first_chunk(
        self,
        model: str,
        call_kwargs: dict[str, Any],
        first_chunk_timeout: float,
        *,
        total_budget: float | None = None,
        abort_event: asyncio.Event | None = None,
    ) -> tuple[Any, Any]:
        """建连并读取首个 chunk，统一受首字节超时保护。

        首字节超时必须同时包住 _do_completion 和首 chunk 读取：上游"半死连接"
        （TCP 建连成功、请求已发出，但上游既不回数据也不断开）会让 _do_completion
        卡在传输建连/等响应头阶段——既不是 429，也不是连接错误，若 wait_for 仅包
        首个 __anext__() 则因 _do_completion 尚未返回而无法启动，请求会静默挂死
        直到 httpx 层超时。

        Args:
            total_budget: establish 全程伞预算（L3 运行时镜像，秒）。多 key ×
                兜底链的逐跳重试在首块窗内放不下时，子类传入展宽预算
                （跳数 × 首块窗 + 排队 + 余量）——单窗伞会把首跳超时后的全部
                重试饿死（换 key 语义在伞层被静默截断）。缺席 = 单窗（基类
                直连形态）。
            abort_event: 调用方停止信号（可选）。与首块预取全程竞速：等待期间
                置位即中断（cancel 本体 → 内部 except 面关流/释放许可）并抛
                StreamCancelledError——KeyPool 排队+建连+TTFT 不再算进停止延迟。

        Raises:
            WireTimeoutError: 首字节超时；或建连成功但零 chunk（过早 EOF，
                按首 token 失败处理——此时底层流已在 _open_and_first_chunk 内关闭）。
            StreamCancelledError: abort_event 置位（调用方停止）。
        """
        umbrella = max(total_budget, first_chunk_timeout) if total_budget else first_chunk_timeout
        try:
            return await _await_with_escape(
                self._open_and_first_chunk(model, call_kwargs),
                umbrella,
                what=f"first chunk (incl. connect) model={model}",
                abort_event=abort_event,
            )
        except StopAsyncIteration:
            # 空流：建连成功但首字节即 EOF（零 chunk），按首 token 失败处理。
            # resp 已在 _open_and_first_chunk 内部 aclose，此处无需再关。
            logger.warning(
                "[%s] STREAM EMPTY: 首字节即空流 (建连成功但零 chunk) model=%s，按首 token 失败处理",
                type(self).__name__,
                model,
            )
            raise llm_wire.WireTimeoutError(
                "Stream first chunk empty: server returned 200 but zero chunks (premature EOF)"
            ) from None
        except TimeoutError:
            logger.error(
                "[%s] STREAM TIMEOUT: establish 全程超时 (%.0fs) 含建连阶段 model=%s",
                type(self).__name__,
                umbrella,
                model,
            )
            raise llm_wire.WireTimeoutError(
                f"Stream first chunk timeout (incl. connect): no response for {umbrella:.0f}s"
            ) from None

    async def _open_and_first_chunk(
        self,
        model: str,
        call_kwargs: dict[str, Any],
    ) -> tuple[Any, Any]:
        """建连并读取首个 chunk，供外层 wait_for 统一限时。

        首个 chunk 读取若抛异常（含 wait_for 超时注入的 CancelledError），
        必须关闭 stream——既为释放 HTTP 连接，也为触发 _bind_release_to_stream
        绑定的 slot.release()，避免并发许可泄漏（建连超时是高频场景）。
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
        # 首块预读面（L5）：KeyPool/兜底链循环已在各自预算窗内预读首 chunk
        # （超时异常在循环内到点，冷却/换 key/兜底链才接得住）——此处直接取用。
        prefetched = getattr(resp, "_prefetched_first_chunk", _NO_PREFETCH)
        if prefetched is not _NO_PREFETCH:
            return resp, prefetched
        try:
            first = await resp.__aiter__().__anext__()
        except BaseException as _first_exc:
            _t2 = _time.monotonic()
            logger.warning(
                "[%s] _open_and_first_chunk: 首chunk异常(%.3fs后) model=%s exc=%s",
                type(self).__name__, _t2 - _t1, model, type(_first_exc).__name__,
            )
            # 超时/异常/取消：关闭流，触发绑定的 release。aclose 自身的任何
            # 异常（含 CancelledError）都不应掩盖/替换原始异常，故全量抑制。
            # ★ 不能裸 await aclose()：半死 SSL socket 会让 aclose 永久
            # 阻塞，把原始异常（超时/取消）吞在 await 里，外层 wait_for 等不到
            # 协程退出就永远不返回 → 引擎死锁。用 _await_with_escape 限时：
            # 正常 aclose 毫秒级完成（同步契约，测试可立即观测关闭）；半死
            # socket 到点即放弃，原始异常照常 raise 透传，残留协程后台回收。
            aclose = getattr(resp, "aclose", None)
            if aclose is not None:
                try:
                    await _await_with_escape(
                        aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="first-chunk aclose",
                    )
                except Exception:
                    # 关闭失败不掩盖正在传播的业务异常；KeyboardInterrupt/
                    # CancelledError 等须穿透以不破坏取消语义
                    pass
            raise
        _t3 = _time.monotonic()
        logger.info(
            "[%s] _open_and_first_chunk: 首chunk到达(%.3fs后) model=%s",
            type(self).__name__, _t3 - _t1, model,
        )
        return resp, first

    async def _consume_stream(
        self,
        response: Any,
        first_chunk: Any,
        state: _StreamState,
        model: str,
        inter_chunk_timeout: float,
        cancel_event: asyncio.Event | None = None,
    ) -> None:
        """消费流：处理首 chunk → 启动心跳/硬超时 → 按 inter-chunk 超时循环消费。

        cancel_event（调用方停止信号，可选）与每个 ``__anext__`` 等待竞速：
        上游静默不吐 chunk 期间置位即中断（抛 StreamCancelledError），停止延迟
        不再取决于下一个 chunk 或 inter_chunk_timeout。流活跃时取消由 on_chunk
        检查点（下一个 chunk 处）接管，两路语义互补。

        首 chunk 已在 _establish_first_chunk 内读取（含建连阶段超时保护），此处直接
        处理它。随后启动两个守护：
        - 心跳探针（_stream_heartbeat）：流静默时持续打 idle 时长 + stream_closed，
          证明接收协程存活（排除接收端死锁），量化上游/传输静默时长；
        - 独立线程硬超时（StreamHardTimeout）：asyncio 心跳/wait_for 共享同一
          event loop，一旦底层 socket 阻塞冻住事件循环会全部失效；硬超时用
          threading 线程倒计时，到点强制 aclose，loop 冻住也能打破死锁。语义为
          "chunk 间隔超时"：每收到一个 chunk 即 reset() 重计时，避免误杀总时长
          长但 chunk 间隔始终健康的流。

        finally 收尾（任何退出路径都执行）：取消心跳、disarm 硬超时、限时 aclose
        底层连接。Windows 半死 SSL socket 会让 httpx aclose 永久阻塞导致本 finally
        不返回（引擎级挂死），故 aclose 用 _await_with_escape 限时：到点放弃关闭，
        残留 socket（CLOSE_WAIT）交由 GC/OS 回收。KeyPoolAdapter 路径下 aclose 已被
        _aclose_with_release 包过一层 wait_for，这里再包一层无害（外层到点会 cancel
        内层）；直连路径下 aclose 是原始的，本层是其唯一保护。
        """
        heartbeat_task: asyncio.Task[None] | None = None
        hard_timeout: StreamHardTimeout | None = None
        try:
            await self._process_chunk(first_chunk, state)
            state.last_chunk_monotonic = _time.monotonic()
            state.chunks_received += 1
            # 启动心跳探针：沿用 process_manager._watchdog_loop 的
            # create_task + CancelledError 退出范式。
            heartbeat_task = asyncio.create_task(
                self._stream_heartbeat(
                    model,
                    inter_chunk_timeout,
                    lambda: _time.monotonic() - state.last_chunk_monotonic,
                    lambda: state.chunks_received,
                    getattr(response, "completion_stream", None),
                )
            )
            hard_timeout = StreamHardTimeout(
                response,
                asyncio.get_running_loop(),
                inter_chunk_timeout,
            )
            hard_timeout.arm()
            # 接收端点诊断：首个 chunk 无论是否携带 tool_calls 都观察 finish_reason
            self._diag_recv_chunk(first_chunk, state, first=True)
            # 后续 chunk：逐次超时，每个 chunk 到达即重置计时器。活跃推理 chunk
            # 间隔远小于 timeout 故不误触发；仅真正静默（死连接）累计满 timeout。
            # 用 _await_with_escape：即使底层 __anext__ 吞掉取消挂死（半死连接），
            # 也能到点抛错透传，而不是被 asyncio.wait_for「等协程退出」卡死。
            # abort_event=cancel_event：停止信号与 __anext__ 竞速，上游静默
            # （不吐 chunk）期间取消即时生效，不干等到下一个 chunk/超时。
            aiter = response.__aiter__()
            while True:
                try:
                    chunk = await _await_with_escape(
                        aiter.__anext__(),
                        inter_chunk_timeout,
                        what=f"inter-chunk model={model}",
                        abort_event=cancel_event,
                    )
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    _idle = _time.monotonic() - state.last_chunk_monotonic
                    logger.warning(
                        "[%s] STREAM TIMEOUT: inter-chunk 静默超时 (%.0fs) 距上个 chunk #%d 已静默 %.0fs model=%s",
                        type(self).__name__,
                        inter_chunk_timeout,
                        state.chunks_received,
                        _idle,
                        model,
                    )
                    raise llm_wire.WireTimeoutError(
                        "Stream inter-chunk timeout:"
                        f" no data for {_idle:.0f}s"
                        f" (last chunk #{state.chunks_received}, timeout={inter_chunk_timeout:.0f}s)"
                    ) from None
                state.last_chunk_monotonic = _time.monotonic()
                state.chunks_received += 1
                # chunk 健康到达：重置硬超时倒计时（chunk 间隔语义，避免误杀长流）
                if hard_timeout is not None:
                    hard_timeout.reset()
                if await self._process_chunk(chunk, state):
                    break
                # ── 接收端点诊断：本 chunk 的 delta.tool_calls / finish_reason 到达情况 ──
                self._diag_recv_chunk(chunk, state, first=False)
        finally:
            # 取消心跳探针任务（避免任务泄漏：超时/异常/正常结束都要清理）
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await heartbeat_task
            # 取消独立线程硬超时（正常结束时不触发强制关闭，幂等）
            if hard_timeout is not None:
                hard_timeout.disarm()
            # 确保超时或异常时关闭 async iterator，释放 HTTP 连接（限时防挂死）
            if hasattr(response, "aclose"):
                try:
                    await _await_with_escape(
                        response.aclose(),
                        _ACLOSE_TIMEOUT_SECONDS,
                        what="response.aclose",
                    )
                except TimeoutError:
                    logger.warning(
                        "[%s] response.aclose finally 超时 %.0fs（半死 socket 放弃关闭），"
                        "残留连接交 GC 回收",
                        type(self).__name__,
                        _ACLOSE_TIMEOUT_SECONDS,
                    )
                except Exception:
                    logger.debug("[%s] response.aclose finally 异常（已忽略）", type(self).__name__)

    async def _process_chunk(self, chunk: Any, state: _StreamState) -> bool:
        """处理单个 chunk（按载荷类型路由），返回是否应中断消费循环。

        先做限频诊断与 usage 核算（与载荷类型无关的公共步骤），再依次分发：
        reasoning_content → 正文/<think/> 状态机 → 工具调用增量。任一载荷要求
        截断（thinking 超限、消费方 stop 信号）即返回 True，跳过本 chunk 的剩余
        载荷与后续所有 chunk。
        """
        self._collect_stream_usage(chunk, state)

        if not chunk.choices:
            return False

        delta = chunk.choices[0].delta

        # wire 层归一 chunk 携带 delta.reasoning_content（上游原生字段直传）
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning and self._append_thinking(reasoning, state, verbose=True, enforce_limit=True):
            return True

        # 文本内容：流式 <think/> 状态机处理（MiniMax 等模型）
        if delta.content and self._handle_delta_content(delta.content, state):
            return True

        if delta.tool_calls:
            self._accumulate_tool_call_deltas(delta.tool_calls, state)
        return False

    def _collect_stream_usage(self, chunk: Any, state: _StreamState) -> None:
        """收集流式 usage（通常出现在最后一个 chunk），记入 state.stream_usage。"""
        if hasattr(chunk, "usage") and chunk.usage:
            _prompt_details = getattr(chunk.usage, "prompt_tokens_details", None)
            state.stream_usage = {
                "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                "total_tokens": getattr(chunk.usage, "total_tokens", 0) or 0,
                "cached_tokens": getattr(_prompt_details, "cached_tokens", 0) or 0,
            }

    def _append_thinking(
        self,
        text: str,
        state: _StreamState,
        *,
        verbose: bool,
        enforce_limit: bool,
    ) -> bool:
        """追加思考片段并发 thinking 事件；返回是否应中断流。

        两条中断路径：消费方 ``on_chunk`` 返回 ``"stop"``（退化循环守卫，任何
        enforce_limit 档位都生效）；思考内容超过 ``max_thinking_chars``
        （仅 enforce_limit 档位检查）。verbose 控制是否打 THINKING 计数 debug
        日志（完整段打，标签切片片段不打，与拆分前各调用点行为一致）。
        """
        state.thinking_parts.append(text)
        if verbose:
            logger.debug(
                "[STREAM][THINKING] #%d +%d chars",
                len(state.thinking_parts),
                len(text),
            )
        if state.on_chunk and state.on_chunk({"type": "thinking", "content": text}) == "stop":
            # 消费方要求停止（退化循环守卫）：思考通道与正文通道同等对待。
            # 此前该返回值被丢弃——思考通道的 "stop" 形同废纸（2026-11-07
            # 事故：思考原地刷同一 emoji 452 次、13 分钟无人拦断）。
            state.stream_repetition = True
            return True
        if not enforce_limit:
            return False
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
        return False

    def _emit_text(self, text: str, state: _StreamState) -> bool:
        """追加正文片段并发 text 事件；返回消费方是否要求停止（"stop" 信号）。"""
        state.result_parts.append(text)
        if not state.on_chunk:
            return False
        return state.on_chunk({"type": "text", "content": text}) == "stop"

    def _strip_think_close(self, seg: str, state: _StreamState) -> str | None:
        """处理确定含 ``</think`` 的文本段：闭合标记前的部分计入 thinking 通道。

        返回闭标签 ``>`` 之后剩余的正文片段（可能为空串/空白，由调用方决定是否
        发正文事件）；思考段触发消费方停止时返回 ``None``（调用方据此中断流，
        残余正文不再发事件）。``in_think_tag`` 标志由调用方维护。开标签与闭
        标签同 chunk 到达、跨 chunk 切分两种路径共用本方法。
        """
        close_idx = seg.index("</think")
        head = seg[:close_idx]
        if head and self._append_thinking(head, state, verbose=False, enforce_limit=False):
            state.in_think_tag = False
            return None
        tail = seg[close_idx:]
        gt = tail.find(">")
        return tail[gt + 1 :] if gt >= 0 else ""

    def _handle_delta_content(self, content: str, state: _StreamState) -> bool:
        """按流式 ``<think/>`` 标签状态机路由 delta.content，返回是否应中断流。

        MiniMax 等模型的思考内容以 ``<think/>`` 标签包裹在 delta.content 中返回，
        且标签会跨多个 chunk 切分。状态机通过 "<think" / "</think" 字符串查找跟踪
        开/闭状态，确保 thinking 内容路由 thinking 通道、正文路由 text 通道。

        中断条件（现状契约，仅两处）：thinking 内容超过 max_thinking_chars；
        正文路径消费方返回 "stop"。开标签前的前缀片段只发事件不检查停止信号；
        标签切片出的 thinking 片段不做截断检查——均沿用拆分前实现。
        """
        if state.in_think_tag:
            # 标签内：检查闭合标签
            if "</think" in content:
                rest = self._strip_think_close(content, state)
                state.in_think_tag = False
                if rest is None:
                    return True
                if rest.strip() and self._emit_text(rest, state):
                    state.stream_repetition = True
                    return True
            else:
                return self._append_thinking(content, state, verbose=True, enforce_limit=True)
        elif "<think" in content:
            # 标签外：检查开标签
            open_idx = content.index("<think")
            before = content[:open_idx]
            if before:
                # 现状契约：开标签前的前缀发 text 事件但不检查停止信号
                self._emit_text(before, state)
            after_open = content[open_idx:]
            gt = after_open.find(">")
            inner = after_open[gt + 1 :] if gt >= 0 else ""
            state.in_think_tag = True
            if "</think" in inner:
                rest = self._strip_think_close(inner, state)
                state.in_think_tag = False
                if rest is None:
                    return True
                if rest.strip() and self._emit_text(rest, state):
                    state.stream_repetition = True
                    return True
            elif inner:
                return self._append_thinking(inner, state, verbose=True, enforce_limit=False)
        else:
            # 纯正文：先收口已开的思考段再发正文事件
            if state.on_chunk and state.thinking_parts:
                state.on_chunk({"type": "thinking_end", "content": ""})
            state.result_parts.append(content)
            logger.debug(
                "[STREAM][TEXT] #%d +%d chars: %s",
                len(state.result_parts),
                len(content),
                repr(content[:80]),
            )
            if state.on_chunk and state.on_chunk({"type": "text", "content": content}) == "stop":
                state.stream_repetition = True
                logger.warning(
                    "[%s] 收到 stop 信号，截断流式输出",
                    type(self).__name__,
                )
                return True
        return False

    def _accumulate_tool_call_deltas(self, tool_calls: Any, state: _StreamState) -> None:
        """合并流式工具调用增量（同 index 的 id/name/arguments 逐段拼接）并发事件。"""
        # thinking→tool_calls 过渡：发送 thinking_end 确保思考完整关闭后再输出工具卡片
        if state.on_chunk and state.thinking_parts:
            state.on_chunk({"type": "thinking_end", "content": ""})
        for tc in tool_calls:
            idx = tc.index if hasattr(tc, "index") else 0
            tc_id = getattr(tc, "id", None)
            if idx not in state.tool_calls_map:
                state.tool_calls_map[idx] = {
                    "id": (tc_id or f"tc_{idx}_{id(state.tool_calls_map)}"),
                    # id 占位标记：首个 chunk 无 id 时先记占位，迟到的真实 id
                    # 覆盖——单一关联键（流事件取首个非空 chunk id，最终结果
                    # 必须同键，占位不覆盖会让同一工具调用以两个 id 分叉）
                    "_id_fabricated": not tc_id,
                    "name": "",
                    "arguments": "",
                }
                logger.debug(
                    "[STREAM][TOOL_CALL] #%d new: id=%s",
                    idx,
                    state.tool_calls_map[idx]["id"],
                )
            elif tc_id and state.tool_calls_map[idx].get("_id_fabricated"):
                logger.debug(
                    "[STREAM][TOOL_CALL] #%d late id adopted: %s → %s",
                    idx,
                    state.tool_calls_map[idx]["id"],
                    tc_id,
                )
                state.tool_calls_map[idx]["id"] = tc_id
                state.tool_calls_map[idx]["_id_fabricated"] = False
            if tc.function:
                if tc.function.name:
                    state.tool_calls_map[idx]["name"] += tc.function.name
                    logger.debug(
                        "[STREAM][TOOL_CALL] #%d name=%s",
                        idx,
                        state.tool_calls_map[idx]["name"],
                    )
                if tc.function.arguments:
                    state.tool_calls_map[idx]["arguments"] += tc.function.arguments
                    _arg_len = len(state.tool_calls_map[idx]["arguments"])
                    logger.debug(
                        "[STREAM][TOOL_CALL] #%d args +%d → %d chars: %s",
                        idx,
                        len(tc.function.arguments),
                        _arg_len,
                        repr(tc.function.arguments[:100]),
                    )

        if state.on_chunk:
            state.on_chunk(
                {
                    "type": "tool_call",
                    "tool_calls": tool_calls,
                }
            )

    def _diag_recv_chunk(self, chunk: Any, state: _StreamState, *, first: bool) -> None:
        """接收端点观察：tool_calls 计数 + finish_reason 观察值（跨界契约保持）。

        复制锚点：源 adapter.py ``_diag_recv_chunk``。不对称是现状契约（勿顺手
        "修复"）：首 chunk 无论是否携带 tool_calls 都更新 finish_reason 观察值；
        后续 chunk 仅在同 chunk 携带 tool_calls 时才更新。日志细节裁掉（诊断面
        归网关），观察语义逐行保留——``finish_reason`` 是上层截断判定的功能输入。
        """
        state.recv_seq += 1
        try:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is not None:
                delta = getattr(choice, "delta", None)
                finish = getattr(choice, "finish_reason", None)
                tcs = getattr(delta, "tool_calls", None) if delta else None
                if tcs:
                    state.recv_tc_count += 1
                if first:
                    if finish:
                        state.finish_reason = finish
                elif tcs:
                    state.finish_reason = finish
        except Exception as exc:  # noqa: BLE001 —— 观察面尽力而为，不影响流处理
            logger.debug("[llm_stream] 接收端点观察异常（不影响流处理）: %s: %s", type(exc).__name__, exc)


    def _build_streaming_response(self, state: _StreamState) -> LLMResponse:
        """拼接累积片段、归一工具调用，构造最终 LLMResponse。

        复制锚点：源 adapter.py ``_build_streaming_response``（拼接/归一语义
        同源；速度统计与统计日志为诊断面，不随复制）。退化循环截断没有上游
        finish_reason（流被我们主动掐断）：收敛为 "length"（协议五值里的截断
        语义），上层按截断轮处理而非当正常收尾。
        """
        result_text = "".join(state.result_parts) if state.result_parts else None
        thinking_text = "".join(state.thinking_parts) if state.thinking_parts else None
        tool_calls = self._normalize_tool_calls(state.tool_calls_map)

        finish_reason = state.finish_reason
        if state.stream_repetition and finish_reason is None:
            finish_reason = "length"

        return LLMResponse(
            text=result_text,
            tool_calls=tool_calls,
            thinking_text=thinking_text,
            usage=state.stream_usage,
            stream_repetition=state.stream_repetition,
            thinking_truncated=state.thinking_truncated,
            finish_reason=finish_reason,
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
            completion_stream: 底层流对象（读 is_closed）
        """
        half = inter_chunk_timeout / 2
        try:
            while True:
                await asyncio.sleep(30.0)
                idle = idle_getter()
                received = chunks_getter()
                closed = getattr(completion_stream, "is_closed", None) if completion_stream is not None else None
                logger.log(
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

    @staticmethod
    def _recover_minimax_text_tool_calls(response: LLMResponse, model: str) -> None:
        """正文原生工具调用兜底：结构化 tool_calls 缺席时从正文解析（原地改写）。

        仅 minimax 模型启用；上游已返回结构化 tool_calls 时不解析（不重复）。
        解析成功即改写 response（tool_calls 补入、正文剥离调用块），并留痕
        warning——这是上游未激活 tool-call 解析的信号，不是常规路径。
        """
        if response.tool_calls or not response.text:
            return
        if "minimax" not in model.lower():
            return
        tool_calls, cleaned = parse_minimax_native_tool_calls(response.text)
        if not tool_calls:
            return
        logger.warning(
            "[adapter] minimax 工具调用以正文标记到达（结构化 tool_calls 缺席），"
            "已解析 %d 个并从正文剥离 | model=%s names=%s",
            len(tool_calls),
            model,
            [tc["name"] for tc in tool_calls],
        )
        response.tool_calls = tool_calls
        response.text = cleaned or None

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
