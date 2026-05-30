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


class _DeadCheckable(Protocol):
    """支持连接死亡检测的 sink 协议。

    注意: 使用 hasattr 而非 isinstance 进行运行时检查，
    因为 unittest.mock 创建的 spec 对象可能无法通过
    runtime_checkable Protocol 的 isinstance 检查。
    """

    @property
    def is_dead(self) -> bool:
        """返回 sink 是否已死亡（连续推送失败超过阈值）。"""
        ...


class TargetedSink:
    """定向输出目标，按 thread_id 直接路由事件到对应 WebSocket 连接。

    路由失败时记录错误并返回 False，不广播。
    广播是消息串扰的根因，已被删除。

    BUG-FIX-fix_20260524_ws_push_fail_frontend_stuck:
    增加连续失败检测：当连续推送失败超过阈值时标记 sink 为 dead，
    上层 drain_loop 检测到 dead 后会提前发送 stream_end 并退出，
    避免前端因收不到 stream_end 而无限等待。
    """

    # 连续推送失败超过此阈值时标记 sink 为 dead
    _MAX_CONSECUTIVE_FAILURES: int = 5

    def __init__(self, notifier: Any, thread_id: str) -> None:
        """初始化定向输出目标。

        Args:
            notifier: 具备 send_to_thread 和 send_to_user 方法的通知器对象
            thread_id: 目标会话的 ws_thread_id（通过 pipeline_thread_map 映射得到）
        """
        self._notifier = notifier
        self._thread_id = thread_id
        self._fail_count: int = 0
        self._is_dead: bool = False

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    @property
    def is_dead(self) -> bool:
        """返回 sink 是否已死亡（连续推送失败超过阈值）。

        当 is_dead 为 True 时，上层应停止尝试发送并尽早发送 stream_end 保底事件。
        """
        return self._is_dead

    async def send_event(self, event: dict) -> bool:
        """通过 WebSocket 推送事件。

        事件中已包含 pipeline_id，前端按 pipeline_id 路由。
        连续失败超过阈值时标记 sink 为 dead。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        try:
            ok = await self._notifier.send_to_thread(self._thread_id, event)
            if ok:
                self._fail_count = 0
                return True
            self._fail_count += 1
            if self._fail_count <= 3:
                logger.error(
                    "TargetedSink: 推送失败 #%d thread_id=%s type=%s pipeline=%s",
                    self._fail_count,
                    (self._thread_id or "(empty)")[:12],
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            self._check_dead()
            return False
        except Exception:
            self._fail_count += 1
            logger.error(
                "TargetedSink: 推送异常 #%d thread_id=%s type=%s",
                self._fail_count,
                (self._thread_id or "(empty)")[:12],
                event.get("type", "?"),
                exc_info=True,
            )
            self._check_dead()
            return False

    def _check_dead(self) -> None:
        """检查连续失败次数是否超过阈值，超过则标记 sink 为 dead。"""
        if not self._is_dead and self._fail_count >= self._MAX_CONSECUTIVE_FAILURES:
            self._is_dead = True
            logger.warning(
                "TargetedSink: 连续推送失败 %d 次，标记 sink 为 dead: "
                "thread_id=%s（前端可能已断开连接）",
                self._fail_count,
                self._thread_id[:12] if self._thread_id else "(empty)",
            )


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
        # BUG-FIX-fix_20260529_msg_order: 缓存 PipelineEntry 引用，避免 Registry unregister 后丢失计数器
        self._entry: Any | None = None
        try:
            from pipeline.registry import get_engine_registry
            self._entry = get_engine_registry().get(pipeline_id)
        except Exception:
            pass

        # 内部状态
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._chunk_event: asyncio.Event = asyncio.Event()
        self._thinking_active: bool = False
        self._accumulated_content: list[str] = []
        self._thinking_content_parts: list[str] = []
        self._stream_started: bool = False
        self._pending_notifications: list[dict] = []

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
        self._part_seq = 0
        self._current_msg_seq = 0
        # BUG-FIX-fix_20260529_notification_lost:
        # 不清空 _pending_notifications，保留给新 drain_loop 在 stream_start 后刷出。
        # 旧 drain_loop 已被 stop+cancel，如果此时清空会丢失缓冲中的通知。
        # self._pending_notifications = []
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if message_id:
            self.message_id = message_id

    # BUG-FIX-fix_20260529_msg_order: 从 PipelineEntry 共享计数器获取下一个 sequence
    def _get_next_sequence(self) -> int:
        """从 PipelineEntry 共享计数器获取下一个 sequence。

        优先使用创建时缓存的 entry 引用，避免 Registry unregister 后丢失计数器。

        Returns:
            全局递增的 sequence 值；entry 不可用时返回 0（降级处理）
        """
        if getattr(self, '_entry', None) is not None:
            return self._entry.next_sequence()
        try:
            from pipeline.registry import get_engine_registry
            entry = get_engine_registry().get(self.pipeline_id)
            if entry is not None:
                self._entry = entry
                return entry.next_sequence()
        except Exception:
            pass
        return 0

    def _next_part_seq(self) -> int:
        """Part 级 sequence，本地递增，仅用于前端 parts 排序。"""
        if not hasattr(self, '_part_seq'):
            self._part_seq = 0
        self._part_seq += 1
        return self._part_seq

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

        BUG-FIX-fix_20260524_ws_push_fail_frontend_stuck:
        当 output_sink 是 TargetedSink 且已被标记为 dead 时，
        跳过发送并直接返回 False，避免无意义的推送尝试。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        # 检查 sink 是否已死亡（连续推送失败超过阈值）
        if getattr(self.output_sink, 'is_dead', False) is True:
            return False

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
        self._current_msg_seq = 0
        logger.info(
            "DEBUG _send_stream_start: msg=%s pipeline=%s sink=%s sink_type=%s",
            self.message_id[:12], self.pipeline_id[:12],
            getattr(self.output_sink, 'sink_id', '?'), type(self.output_sink).__name__,
        )
        success = await self._send_event(self._make_event("stream_start", {
            "message_id": self.message_id,
            "pipeline_id": self.pipeline_id,
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
        logger.debug(
            "_handle_chunk: type=%s content_len=%d pipeline=%s msg=%s",
            chunk_type, len(content) if content else 0,
            self.pipeline_id[:12], self.message_id[:12],
        )

        if chunk_type == "text" and content:
            self._accumulated_content.append(content)
            await self._send_event(self._make_event("stream_chunk", {
                "content": content,
                "sequence": self._next_part_seq(),
            }))

        elif chunk_type == "thinking" and content:
            self._thinking_content_parts.append(content)
            if not self._thinking_active:
                self._thinking_active = True
                await self._send_event(self._make_event("thinking_start", {
                    "sequence": self._next_part_seq(),
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
            _seq = self._next_part_seq()
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
                _fixup_seq = self._next_part_seq()
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

        try:
            # 0.5 刷出 reset_for_new_turn 保留的缓冲通知（在 stream_start 之前）
            if self._pending_notifications:
                _preserved = self._pending_notifications[:]
                self._pending_notifications = []
                for _notif in _preserved:
                    await self._send_event(self._make_event("system_notification", _notif))
                logger.info(
                    "drain_loop: flushed %d preserved notifications before stream_start: pipeline=%s",
                    len(_preserved), self.pipeline_id[:12],
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
                # BUG-FIX-fix_20260524_ws_push_fail_frontend_stuck:
                # 检测 sink 是否已死亡（连续推送失败超过阈值），
                # 如果 dead 则提前发送 stream_end 保底事件并退出循环，
                # 避免前端因收不到 stream_end 而无限等待。
                if getattr(self.output_sink, 'is_dead', False) is True:
                    logger.warning(
                        "drain_loop: sink 已 dead（前端连接丢失），提前终止流式输出: "
                        "pipeline=%s chunks=%d",
                        self.pipeline_id[:12], _chunk_count,
                    )
                    await self._close_thinking_if_active(None)
                    full_content = "".join(self._accumulated_content)
                    # sink 已 dead，stream_end 大概率也发不出去，但仍尝试发送
                    try:
                        await self.output_sink.send_event(self._make_event("stream_end", {
                            "full_content": full_content,
                            "connection_lost": True,
                        }))
                    except Exception:
                        pass
                    # 丢弃缓冲的通知（连接已死，通知无需发送）
                    self._pending_notifications = []
                    return {
                        "accumulated_content": full_content,
                        "thinking_content_parts": list(self._thinking_content_parts),
                        "connection_lost": True,
                        "timed_out": False,
                    }

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
                        # BUG-FIX-fix_20260528_drain_race:
                        # 问题根因: inject_message 设置 _wake_event 后，引擎的
                        #   _suspend_and_wait 恢复逻辑（设 is_suspended=False）
                        #   需要下一个事件循环 tick 才执行。如果 drain_loop 在
                        #   此处立即退出，LLM 后续生成的 chunk 无消费者。
                        # 修复方案: 检测到 suspended 后等待最多 1 秒，期间每
                        #   50ms 重新检查。如果引擎醒来则继续；超时才退出。
                        _suspend_grace_start = asyncio.get_event_loop().time()
                        _suspend_grace_max = 1.0
                        _actually_suspended = True
                        while asyncio.get_event_loop().time() - _suspend_grace_start < _suspend_grace_max:
                            await asyncio.sleep(0.05)
                            if not suspend_check():
                                _actually_suspended = False
                                break
                            if not self._queue.empty():
                                _actually_suspended = False
                                break
                        if _actually_suspended:
                            logger.info(
                                "drain_loop: engine suspended (grace expired), ending stream: pipeline=%s chunks=%d",
                                self.pipeline_id[:12], _chunk_count,
                            )
                            break
                        last_keepalive = asyncio.get_event_loop().time()

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
                                # BUG-FIX-fix_20260529_notification_lost:
                                # 超时退出时刷出缓冲通知，避免丢失
                                if self._pending_notifications:
                                    for _notif in self._pending_notifications:
                                        try:
                                            await self._send_event(self._make_event("system_notification", _notif))
                                        except Exception:
                                            pass
                                    self._pending_notifications = []
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
                        "thread_id": getattr(self.output_sink, '_thread_id', '') or "",
                    }))
                    logger.info(
                        "drain_loop: pipeline_suspended → state_change sent: pipeline=%s",
                        self.pipeline_id[:12],
                    )
                    continue

                if _chunk_type == "system":
                    _notif_content = chunk.get("content", "")
                    self._pending_notifications.append({
                        "content": _notif_content,
                        "level": chunk.get("level", "info"),
                        "notificationType": chunk.get("notificationType", ""),
                    })
                    logger.debug(
                        "drain_loop: system_notification buffered: pipeline=%s count=%d",
                        self.pipeline_id[:12], len(self._pending_notifications),
                    )
                    logger.info(
                        "[DIAG] drain_loop: system chunk buffered: pipeline=%s queue_len=%d "
                        "content=%.60s chunk_count=%d",
                        self.pipeline_id[:12], len(self._pending_notifications),
                        _notif_content[:60], _chunk_count,
                    )
                    continue

                if _chunk_type in ("tool_start", "tool_result"):
                    logger.debug(
                        "drain: type=%s tool=%s pipeline=%s",
                        _chunk_type, chunk.get('tool_name'), self.pipeline_id[:12],
                    )

                _chunk_count += 1
                logger.info(
                    "drain_loop chunk #%d: type=%s content_len=%d pipeline=%s",
                    _chunk_count, _chunk_type,
                    len(chunk.get("content", "")) if chunk.get("content") else 0,
                    self.pipeline_id[:12],
                )
                try:
                    await self._handle_chunk(chunk)
                except Exception as _hc_err:
                    logger.warning(
                        "drain_loop: _handle_chunk 异常 (chunk #%d type=%s): %s pipeline=%s",
                        _chunk_count, _chunk_type, _hc_err, self.pipeline_id[:12],
                    )

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

            # 5. 刷出缓冲的系统通知（stream_end 后发送，确保前端已标记消息完成）
            if self._pending_notifications:
                _notif_count = len(self._pending_notifications)
                for _notif_idx, _notif in enumerate(self._pending_notifications):
                    logger.info(
                        "[DIAG] drain_loop: flushing notification [%d/%d]: pipeline=%s content=%.60s",
                        _notif_idx + 1, _notif_count, self.pipeline_id[:12],
                        _notif.get("content", "")[:60],
                    )
                    await self._send_event(self._make_event("system_notification", _notif))
                self._pending_notifications = []
                logger.info(
                    "drain_loop: flushed %d buffered system_notification: pipeline=%s",
                    _notif_count, self.pipeline_id[:12],
                )

            return {
                "accumulated_content": full_content,
                "thinking_content_parts": list(self._thinking_content_parts),
                "timed_out": False,
            }
        except Exception as _dl_err:
            # BUG-FIX-fix_20260529_frontend_stuck:
            # drain_loop 异常退出时补发 stream_end，防止前端永远卡在"思考中"
            logger.error(
                "drain_loop 异常退出: pipeline=%s error=%s",
                self.pipeline_id[:12], _dl_err, exc_info=True,
            )
            if self._stream_started:
                try:
                    await self._close_thinking_if_active(None)
                    _fallback_content = "".join(self._accumulated_content)
                    await self._send_event(self._make_event("stream_end", {
                        "full_content": _fallback_content,
                        "error": True,
                    }))
                except Exception:
                    pass
            return {
                "accumulated_content": "".join(self._accumulated_content),
                "thinking_content_parts": list(self._thinking_content_parts),
                "timed_out": False,
            }
        finally:
            # BUG-FIX-fix_20260529_notification_order:
            # drain_loop 退出后重置 _stream_started，防止 send_pipeline_message
            # 误判为仍在流式而将通知缓冲到无消费者的 _pending_notifications。
            self._stream_started = False

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

        logger.info(
            "[DIAG] send_new_message BEFORE send: pipeline=%s msg=%s "
            "sequence=%d content=%.60s",
            self.pipeline_id[:12], self.message_id[:12],
            sequence, effective_content[:60],
        )

        await self._send_event(self._make_event("new_message", {
            "id": self.message_id,
            "role": "assistant",
            "content": effective_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
        }))

        logger.info(
            "[DIAG] send_new_message AFTER send: pipeline=%s msg=%s sequence=%d",
            self.pipeline_id[:12], self.message_id[:12], sequence,
        )


# ---------------------------------------------------------------------------
# 模块级统一出口函数
# ---------------------------------------------------------------------------

async def send_frontend_event(
    pipeline_id: str,
    event: dict,
) -> bool:
    """通过统一出口发送前端事件（基于管道ID查找）。

    通过 pipeline_id 从 EngineRegistry 获取已有 bridge，直接用其
    output_sink 发送事件。无 bridge 时回退到从 ServiceProvider
    获取 notifier + 从 registry 获取 thread_id 创建临时 sink。

    Args:
        pipeline_id: 管道 ID，所有前端推送都通过管道ID标识
        event: 要发送的事件字典，格式 {"type": ..., "data": ...}

    Returns:
        发送成功返回 True，失败返回 False
    """
    if not pipeline_id:
        return False

    from pipeline.registry import get_engine_registry
    registry = get_engine_registry()

    bridge = registry.get_bridge(pipeline_id)
    if bridge is not None:
        return await bridge._send_event(event)

    entry = registry.get(pipeline_id)
    if entry is None or not entry.thread_id:
        return False

    try:
        from ws_handler import ws_interaction_notifier as _notifier
    except Exception:
        _notifier = None

    if not _notifier:
        return False

    sink = TargetedSink(_notifier, entry.thread_id)
    return await sink.send_event(event)
