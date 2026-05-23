"""管道流式事件桥接模块。

将 engine 的 on_chunk 同步回调转换为前端 WebSocket 协议事件，
通过 IOutputSink 抽象统一发送到 TargetedSink（按 thread_id 定向路由）。
消除了 start_server.py 和 task_worker.py 中约 300 行重复的流式事件发送逻辑。
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# IOutputSink 协议与实现
# ---------------------------------------------------------------------------

@runtime_checkable
class IOutputSink(Protocol):
    """输出目标协议，抽象 WebSocket 直连与广播两种发送方式。"""

    async def send_event(self, event: dict) -> bool:
        """发送事件到前端，成功返回 True，失败返回 False。"""
        ...

    @property
    def sink_id(self) -> str:
        """返回输出目标的唯一标识，用于日志和调试。"""
        ...



class TargetedSink:
    """定向输出目标，按 thread_id 直接路由事件到对应 WebSocket 连接。

    路由失败时记录错误并返回 False，不广播。
    广播是消息串扰的根因，已被删除。
    """

    def __init__(self, notifier: Any, thread_id: str) -> None:
        """初始化定向输出目标。

        Args:
            notifier: 具备 send_to_thread 和 send_to_user 方法的通知器对象
            thread_id: 目标会话的 ws_thread_id（通过 pipeline_thread_map 映射得到）
        """
        self._notifier = notifier
        self._thread_id = thread_id
        self._fail_count: int = 0

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    async def send_event(self, event: dict) -> bool:
        """直接路由事件到指定 thread_id 的 WebSocket 连接。

        路由失败时记录错误，不广播。广播会导致消息串扰。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        if not self._thread_id:
            self._fail_count += 1
            if self._fail_count <= 3:
                logger.error(
                    "TargetedSink: thread_id 为空，事件丢失 #%d type=%s pipeline=%s",
                    self._fail_count,
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            return False

        try:
            ok = await self._notifier.send_to_thread(self._thread_id, event)
            if ok:
                return True
            self._fail_count += 1
            if self._fail_count <= 3:
                logger.error(
                    "TargetedSink: 定向推送失败 #%d thread_id=%s type=%s pipeline=%s",
                    self._fail_count,
                    self._thread_id[:12],
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            return False
        except Exception:
            self._fail_count += 1
            logger.error(
                "TargetedSink: 推送异常 #%d thread_id=%s type=%s err=%s",
                self._fail_count,
                self._thread_id[:12],
                event.get("type", "?"),
                exc_info=True,
            )
            return False


# ---------------------------------------------------------------------------
# PipelineStreamBridge 核心类
# ---------------------------------------------------------------------------

class PipelineStreamBridge:
    """管道流式事件桥接器，将 engine 的 on_chunk 回调桥接到 IOutputSink。

    核心职责：
    1. 提供 on_chunk 同步回调供 engine.run() 使用
    2. 通过 drain_loop 异步消费队列、格式化事件、经 sink 发送到前端
    3. 管理 thinking 状态追踪、文本累积、心跳保活、挂起超时检测
    """

    def __init__(
        self,
        pipeline_id: str,
        output_sink: IOutputSink,
        message_id: str | None = None,
    ) -> None:
        """初始化管道流式桥接器。

        Args:
            pipeline_id: 管道 ID，附加到每个事件的 data 中
            output_sink: 输出目标，负责实际发送事件
            message_id: 消息 ID，不传则自动生成
        """
        self.pipeline_id = pipeline_id
        self.output_sink = output_sink
        self.message_id = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        self._sent_tool_starts: set[str] = set()
        # BUG-FIX-fix_20260522_tool_order: 统一自增序号，text/tool 共用，确保前端按执行顺序渲染
        self._seq: int = 0

        # 内部状态
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._chunk_event: asyncio.Event = asyncio.Event()
        self._thinking_active: bool = False
        self._accumulated_content: list[str] = []
        self._thinking_content_parts: list[str] = []
        self._stream_started: bool = False

    def on_chunk(self, chunk: dict) -> None:
        """同步回调，将 chunk 放入内部队列并立即唤醒 drain_loop。

        此方法直接传给 engine.run(on_chunk=bridge.on_chunk)，
        由 LLM Adapter 在流式生成时同步调用。

        Args:
            chunk: 管道事件字典，包含 type 和 content 等字段
        """
        _ct = chunk.get("type", "?")
        if _ct in ("tool_start", "tool_result"):
            logger.debug(
                "on_chunk: type=%s tool=%s msg=%s pipeline=%s",
                _ct, chunk.get('tool_name'),
                self.message_id[:12], self.pipeline_id[:12],
            )
        self._queue.put_nowait(chunk)
        self._chunk_event.set()

    def stop(self) -> None:
        """发送哨兵值 None 终止 drain_loop。"""
        self._queue.put_nowait(None)

    def reset_for_new_turn(self, message_id: str | None = None) -> None:
        """重置内部状态，为新的一轮对话做准备。

        在引擎挂起后唤醒时调用，确保新 turn 的流式输出
        不会包含上一 turn 的残留内容。

        Args:
            message_id: 新的消息 ID，不传则保留当前值
        """
        self._accumulated_content = []
        self._thinking_content_parts = []
        self._thinking_active = False
        self._stream_started = False
        self._sent_tool_starts = set()
        self._seq = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if message_id:
            self.message_id = message_id

    def _make_event(self, event_type: str, data: dict) -> dict:
        """构造事件字典，自动注入 pipeline_id 和 message_id。

        使用 setdefault 避免覆盖调用方显式传入的值。

        Args:
            event_type: 事件类型字符串
            data: 事件的 data 字段内容

        Returns:
            完整的事件字典 {"type": ..., "data": ...}
        """
        data.setdefault("pipeline_id", self.pipeline_id)
        data.setdefault("message_id", self.message_id)
        return {"type": event_type, "data": data}

    async def _send_event(self, event: dict) -> bool:
        """通过 output_sink 发送事件，记录发送失败日志。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        success = await self.output_sink.send_event(event)
        if not success:
            logger.debug(
                "事件发送失败: sink=%s, event_type=%s",
                self.output_sink.sink_id,
                event.get("type", "unknown"),
            )
        return success

    async def _send_stream_start(self) -> None:
        """发送 stream_start 事件，通知前端开始接收流式输出。

        BUG-FIX-fix_20260523_pipeline_id_in_events:
        确保 stream_start 事件中 pipeline_id 始终存在，
        _threadId 仅作为辅助路由信息。
        """
        self._stream_started = True
        _start_seq = self._seq
        self._seq += 1
        logger.info(
            "DEBUG _send_stream_start: msg=%s pipeline=%s sink=%s sink_type=%s seq=%d",
            self.message_id[:12], self.pipeline_id[:12],
            getattr(self.output_sink, 'sink_id', '?'), type(self.output_sink).__name__,
            _start_seq,
        )
        success = await self._send_event(self._make_event("stream_start", {
            "message_id": self.message_id,
            "pipeline_id": self.pipeline_id,
            "sequence": _start_seq,
            "_threadId": getattr(self.output_sink, '_thread_id', None),
        }))
        logger.info(
            "DEBUG _send_stream_start result: success=%s msg=%s pipeline=%s",
            success, self.message_id[:12], self.pipeline_id[:12],
        )

    async def _close_thinking_if_active(self, duration_ms: Any = None) -> None:
        """如果 thinking 处于活跃状态，发送 thinking_end 事件关闭。

        Args:
            duration_ms: thinking 持续时间（毫秒），可为 None
        """
        if self._thinking_active:
            self._thinking_active = False
            await self._send_event(self._make_event("thinking_end", {
                "duration_ms": duration_ms,
            }))

    async def _handle_chunk(self, chunk: dict) -> None:
        """处理单个 chunk 事件，转换为前端协议格式并发送。

        Args:
            chunk: 包含 type 和 content 等字段的管道事件字典
        """
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")

        if chunk_type == "text" and content:
            self._accumulated_content.append(content)
            _text_seq = self._seq
            self._seq += 1
            await self._send_event(self._make_event("stream_chunk", {
                "content": content,
                "sequence": _text_seq,
            }))

        elif chunk_type == "thinking" and content:
            self._thinking_content_parts.append(content)
            if not self._thinking_active:
                self._thinking_active = True
                _thinking_seq = self._seq
                self._seq += 1
                await self._send_event(self._make_event("thinking_start", {
                    "sequence": _thinking_seq,
                }))
            await self._send_event(self._make_event("thinking_chunk", {
                "content": content,
            }))

        elif chunk_type == "thinking_end":
            await self._close_thinking_if_active(chunk.get("duration_ms"))

        elif chunk_type == "tool_start":
            await self._close_thinking_if_active(None)
            _pending_text = "".join(self._accumulated_content)
            if _pending_text:
                await self._send_event(self._make_event("stream_end", {
                    "full_content": _pending_text,
                }))
            _call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            self._sent_tool_starts.add(_call_id)
            _seq = self._seq
            self._seq += 1
            logger.info(
                "tool_start: tool=%s call_id=%s seq=%d pipeline=%s",
                chunk.get('tool_name'), _call_id, _seq, self.pipeline_id[:12],
            )
            await self._send_event(self._make_event("tool_start", {
                "tool_name": chunk.get("tool_name", "unknown"),
                "args": chunk.get("args"),
                "call_id": chunk.get("call_id"),
                "sequence": _seq,
            }))

        elif chunk_type == "tool_result":
            _result_call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            if _result_call_id not in self._sent_tool_starts:
                logger.info(
                    "FIXUP: tool_result without tool_start: tool=%s pipeline=%s",
                    chunk.get('tool_name'), self.pipeline_id[:12],
                )
                self._sent_tool_starts.add(_result_call_id)
                _fixup_seq = self._seq
                self._seq += 1
                await self._send_event(self._make_event("tool_start", {
                    "tool_name": chunk.get("tool_name", "unknown"),
                    "args": None,
                    "call_id": chunk.get("call_id"),
                    "sequence": _fixup_seq,
                }))
            await self._send_event(self._make_event("tool_result", {
                "tool_name": chunk.get("tool_name", "unknown"),
                "success": chunk.get("success", True),
                "result": chunk.get("result"),
                "duration_ms": chunk.get("duration_ms"),
                "call_id": chunk.get("call_id"),
            }))
            self._accumulated_content = []
            await self._send_stream_start()

        elif chunk_type == "iteration":
            # 迭代开始时关闭旧的 thinking
            await self._close_thinking_if_active(None)
            await self._send_event(self._make_event("iteration", {
                "iteration": chunk.get("iteration", 0),
                "max_iterations": chunk.get("max_iterations", 0),
            }))

    async def drain_loop(
        self,
        engine_task: asyncio.Task,
        *,
        heartbeat_callback: Callable[[], Coroutine[Any, Any, None]] | None = None,
        heartbeat_interval: float = 5.0,
        suspend_check: Callable[[], bool] | None = None,
        call_timeout: float | None = None,
    ) -> dict:
        """异步消费队列，格式化事件，通过 sink 发送到前端。

        核心消费循环：从内部队列取出 chunk → 转换为前端协议事件 → 经 sink 发送。
        支持心跳保活和挂起超时检测。

        Args:
            engine_task: 管道引擎的异步 Task，用于判断管道是否结束
            heartbeat_callback: 可选的心跳回调协程，在 TimeoutError 时调用
            heartbeat_interval: 心跳间隔秒数，默认 5.0
            suspend_check: 可选的挂起检测函数，返回 True 表示管道已挂起
            call_timeout: 可选的 LLM 活动超时秒数，挂起期间不计入

        Returns:
            dict 包含:
            - accumulated_content: str 累积的完整文本
            - thinking_content_parts: list[str] thinking 内容片段
        """
        logger.info(
            "drain_loop 开始: msg=%s pipeline=%s sink=%s",
            self.message_id[:16], self.pipeline_id[:12], type(self.output_sink).__name__,
        )
        # 1. 发送 stream_start
        try:
            await self._send_stream_start()
            logger.info(
                "drain_loop: stream_start 已发送: msg=%s pipeline=%s sink=%s",
                self.message_id[:12], self.pipeline_id[:12], self.output_sink.sink_id,
            )
        except Exception as _e:
            logger.error("drain_loop: stream_start 失败: %s", _e, exc_info=True)
            return {"accumulated_content": "", "thinking_content_parts": [], "timed_out": False}

        last_keepalive = asyncio.get_event_loop().time()
        # 挂起超时检测需要独立追踪最后活跃时间
        _last_active = asyncio.get_event_loop().time() if call_timeout is not None else 0.0

        # 2. 主循环：消费队列
        _chunk_count = 0
        while not engine_task.done() or not self._queue.empty():
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                now = asyncio.get_event_loop().time()

                _is_suspended = (
                    not engine_task.done()
                    and self._queue.empty()
                    and suspend_check is not None
                    and suspend_check()
                )

                if _is_suspended:
                    logger.info(
                        "drain_loop: engine suspended, ending stream: pipeline=%s chunks=%d",
                        self.pipeline_id[:12], _chunk_count,
                    )
                    break

                # 心跳保活：无论 heartbeat_callback 是否存在，都发送 stream_keepalive
                if now - last_keepalive > heartbeat_interval:
                    last_keepalive = now
                    # 仅在 heartbeat_callback 有值时调用回调
                    if heartbeat_callback is not None:
                        try:
                            await heartbeat_callback()
                        except Exception:
                            pass
                    # keepalive 事件始终发送，防止前端因长时间无 chunk 而超时
                    try:
                        await self._send_event(self._make_event("stream_keepalive", {}))
                    except Exception:
                        pass

                # 挂起超时检测：管道挂起时不计入超时，超时后直接 return
                if call_timeout is not None:
                    if suspend_check is not None and suspend_check():
                        _last_active = now
                    else:
                        elapsed = now - _last_active
                        if elapsed > call_timeout:
                            logger.warning(
                                "LLM 活动超时 (%.1fs/%.1fs): pipeline=%s",
                                elapsed, call_timeout, self.pipeline_id,
                            )
                            # FIX: 超时退出前补发 stream_end，确保前端总是收到配对的 start/end
                            await self._close_thinking_if_active(None)
                            full_content = "".join(self._accumulated_content)
                            try:
                                await self._send_event(self._make_event("stream_end", {
                                    "full_content": full_content,
                                    "timed_out": True,
                                }))
                            except Exception as _send_err:
                                logger.debug("超时 stream_end 发送失败: %s", _send_err)
                            return {
                                "accumulated_content": full_content,
                                "thinking_content_parts": list(self._thinking_content_parts),
                                "timed_out": True,
                            }

                continue

            # 收到 chunk，更新活跃时间
            if call_timeout is not None:
                _last_active = asyncio.get_event_loop().time()

            if chunk is None:
                break

            _chunk_type = chunk.get("type", "?")

            if _chunk_type == "pipeline_suspended":
                await self._close_thinking_if_active(None)
                await self._send_event(self._make_event("state_change", {
                    "status": "suspended",
                    "pipeline_id": chunk.get("pipeline_id", self.pipeline_id),
                    # BUG-FIX-fix_20260523_thread_id_attr:
                    # 问题根因: self._thread_id 在 PipelineStreamBridge 上不存在（仅在 TargetedSink 上），
                    #   管道挂起时访问该属性会抛出 AttributeError。
                    # 修复方案: 使用 getattr 从 output_sink 安全获取 _thread_id。
                    "thread_id": getattr(self.output_sink, '_thread_id', '') or "",
                }))
                logger.info(
                    "drain_loop: pipeline_suspended → state_change sent: pipeline=%s",
                    self.pipeline_id[:12],
                )
                continue

            if _chunk_type in ("tool_start", "tool_result"):
                logger.debug(
                    "drain: type=%s tool=%s pipeline=%s",
                    _chunk_type, chunk.get('tool_name'), self.pipeline_id[:12],
                )

            _chunk_count += 1
            await self._handle_chunk(chunk)

        # 3. 管道结束后关闭可能仍活跃的 thinking
        await self._close_thinking_if_active(None)

        # 4. 发送 stream_end
        full_content = "".join(self._accumulated_content)
        _end_ok = await self._send_event(self._make_event("stream_end", {
            "full_content": full_content,
        }))
        logger.info(
            "drain_loop: stream_end %s: msg=%s pipeline=%s content=%d chars chunks=%d/%d",
            "OK" if _end_ok else "FAILED",
            self.message_id[:12], self.pipeline_id[:12],
            len(full_content), _chunk_count, len(self._accumulated_content),
        )

        return {
            "accumulated_content": full_content,
            "thinking_content_parts": list(self._thinking_content_parts),
            "timed_out": False,
        }

    async def send_new_message(self, content: str, sequence: int = 1) -> None:
        """发送 new_message 最终消息，包含完整的助手消息数据。

        BUG-FIX-20260515: 当 content 为空但流式累积有内容时，
        使用 _accumulated_content 作为保底，防止发送空消息。

        Args:
            content: 消息内容
            sequence: 消息序号，默认为 1
        """
        # BUG-FIX-20260515: 空内容保底逻辑
        # 问题根因: _stream_engine_response 提取 actual_content 时，
        #   引擎返回的 messages 中 assistant 内容可能为空，
        #   导致 send_new_message('') 发送空消息，前端显示空白。
        # 修复: 当传入内容为空但流式累积有内容时，用累积内容保底。
        effective_content = content
        if not effective_content:
            accumulated = "".join(self._accumulated_content)
            if accumulated:
                effective_content = accumulated
                logger.info(
                    "send_new_message: 传入内容为空，使用累积内容保底 "
                    "(%d chars): msg=%s pipeline=%s",
                    len(effective_content),
                    self.message_id[:12],
                    self.pipeline_id[:12],
                )

        await self._send_event(self._make_event("new_message", {
            "id": self.message_id,
            "role": "assistant",
            "content": effective_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
        }))
