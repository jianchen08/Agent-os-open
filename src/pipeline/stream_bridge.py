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
from enum import StrEnum
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 管道消息来源枚举
# ---------------------------------------------------------------------------

class EnvelopeSource(StrEnum):
    LLM = "llm"
    USER = "user"
    SYSTEM = "system"
    TRIGGER = "trigger"
    ENGINE = "engine"


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
    后端执行为主，前端断连不干预引擎运行。
    """

    def __init__(self, notifier: Any, thread_id: str) -> None:
        """初始化定向输出目标。

        Args:
            notifier: 具备 send_to_thread 和 send_to_user 方法的通知器对象
            thread_id: 目标会话的 ws_thread_id（通过 pipeline_thread_map 映射得到）
        """
        self._notifier = notifier
        self._thread_id = thread_id

    @property
    def sink_id(self) -> str:
        """返回定向发送标识。"""
        return f"targeted:{self._thread_id or 'no-thread'}"

    async def send_event(self, event: dict) -> bool:
        """通过 WebSocket 推送事件。

        事件中已包含 pipeline_id，前端按 pipeline_id 路由。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        try:
            ok = await self._notifier.send_to_thread(self._thread_id, event)
            if not ok:
                logger.debug(
                    "TargetedSink: 推送失败 thread_id=%s type=%s pipeline=%s",
                    (self._thread_id or "(empty)")[:12],
                    event.get("type", "?"),
                    (event.get("data", {}).get("pipeline_id") or "?")[:12],
                )
            return ok
        except Exception:
            logger.debug(
                "TargetedSink: 推送异常 thread_id=%s type=%s",
                (self._thread_id or "(empty)")[:12],
                event.get("type", "?"),
                exc_info=True,
            )
            return False


# ---------------------------------------------------------------------------
# MultiChannelSink — 多通道输出分发
# ---------------------------------------------------------------------------

class MultiChannelSink:
    """多渠道输出分发器。将 bridge 产出的内部事件分发给所有注册的通道。

    每个通道实现 IOutputSink 协议，由 MultiChannelSink 统一管理。
    新增通道只需 register，不改 bridge 核心逻辑。
    """

    def __init__(self) -> None:
        self._channels: dict[str, IOutputSink] = {}

    def register(self, name: str, sink: IOutputSink) -> None:
        """注册一个通道。"""
        self._channels[name] = sink
        logger.info("[MultiChannel] registered channel: %s sink=%s", name, sink.sink_id)

    def unregister(self, name: str) -> None:
        """注销一个通道。"""
        self._channels.pop(name, None)

    @property
    def sink_id(self) -> str:
        return f"multi:{','.join(self._channels.keys())}" if self._channels else "multi:empty"

    async def send_event(self, event: dict) -> bool:
        """分发事件给所有通道。任一通道成功即返回 True。"""
        any_success = False
        for name, sink in list(self._channels.items()):
            try:
                if await sink.send_event(event):
                    any_success = True
            except Exception:
                pass
        return any_success


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
        self._container_task_id: str = ""
        try:
            from pipeline.registry import get_engine_registry
            self._entry = get_engine_registry().get(pipeline_id)
            if self._entry and hasattr(self._entry, "tags"):
                self._container_task_id = self._entry.tags.get("task_id", "")
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
        self._collected_parts: list[dict] = []

    def on_chunk(self, chunk: dict) -> None:
        """同步回调：将 chunk 放入队列供 drain_loop 消费。"""
        self._queue.put_nowait(chunk)
        self._chunk_event.set()

    def stop(self) -> None:
        """发送哨兵值 None 终止 drain_loop。"""
        self._queue.put_nowait(None)

    def enqueue_notification(
        self,
        content: str,
        *,
        source: str = "system",
        level: str = "info",
    ) -> int:
        """统一系统通知入口。替代 message_bus 中的多条降级路径。

        系统通知进入 bridge 内部队列，由 drain_loop 在适当时机：
        - 流式中：缓冲到 _pending_notifications，stream_end 后刷出
        - 非流式：stream_end 后刷出
        - LLM注入：通过 engine 回调（如果已注册）

        Args:
            content: 通知文本
            source: 消息来源（system/trigger）
            level: 通知级别（info/warning/error）

        Returns:
            分配的全局序列号，用于前后端一致性校验
        """
        if not content or not content.strip():
            return -1
        _notif_seq = self._get_next_sequence()
        _notif = {
            "type": "notification",
            "content": content.strip(),
            "source": source,
            "level": level,
            "notificationType": f"{source}_notification",
            "notification_id": f"sys_{self.pipeline_id[:8]}_{_notif_seq}",
            "sequence": _notif_seq,
        }
        self._queue.put_nowait(_notif)
        self._chunk_event.set()
        logger.info(
            "[Bridge] enqueue_notification: seq=%d source=%s pipeline=%s content=%.50s",
            _notif_seq, source, self.pipeline_id[:12], content[:50],
        )
        return _notif_seq

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
        self._collected_parts = []
        self._sent_tool_starts = set()
        self._part_seq = 0
        self._current_msg_seq = 0
        # BUG-FIX-fix_20260529_notification_lost:
        # 不清空 _pending_notifications，保留给新 drain_loop 在 stream_start 后刷出。
        # 旧 drain_loop 已被 stop+cancel，如果此时清空会丢失缓冲中的通知。
        # self._pending_notifications = []
        # BUG-FIX-fix_20260530_queue_event_loop:
        # 重建 Queue 和 Event，避免绑定到旧事件循环导致 RuntimeError。
        # 复用 bridge 时（ensure_bridge -> reset_for_new_turn），如果事件循环
        # 已更换（进程重启、uvicorn reload 等），旧 Queue 的 _loop 仍指向旧循环。
        try:
            loop = asyncio.get_running_loop()
            if getattr(self._queue, '_loop', None) is not loop:
                self._queue = asyncio.Queue()
                self._chunk_event = asyncio.Event()
            else:
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
        except RuntimeError:
            self._queue = asyncio.Queue()
            self._chunk_event = asyncio.Event()
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
        """构造事件字典，自动注入 pipeline_id、message_id 和 container_task_id。

        使用 setdefault 避免覆盖调用方显式传入的值。

        Args:
            event_type: 事件类型字符串
            data: 事件的 data 字段内容

        Returns:
            完整的事件字典 {"type": ..., "data": ...}
        """
        data.setdefault("pipeline_id", self.pipeline_id)
        data.setdefault("message_id", self.message_id)
        if self._container_task_id:
            data.setdefault("container_task_id", self._container_task_id)
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

        # 第一个实际内容 chunk 到达时才发 stream_start（延迟发送）
        # 避免空转 drain_loop 产生无主占位符
        if not self._stream_started and chunk_type not in ("system", "notification", "iteration", "pipeline_suspended"):
            await self._send_stream_start()

        content = chunk.get("content", "")
        logger.debug(
            "_handle_chunk: type=%s content_len=%d pipeline=%s msg=%s",
            chunk_type, len(content) if content else 0,
            self.pipeline_id[:12], self.message_id[:12],
        )

        if chunk_type == "text" and content:
            self._accumulated_content.append(content)
            part = {"type": "text", "content": content, "state": "streaming",
                    "sequence": self._next_part_seq()}
            self._collected_parts.append(part)
            await self._send_event(self._make_event("stream_chunk", {
                "content": content,
                "sequence": part["sequence"],
            }))

        elif chunk_type == "thinking" and content:
            self._thinking_content_parts.append(content)
            part = {"type": "thinking", "content": content, "state": "streaming",
                    "sequence": self._next_part_seq()}
            self._collected_parts.append(part)
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

        elif chunk_type == "tool_call":
            # BUG-FIX-fix_20260601_tool_call_chunk_lost:
            # 问题根因: LLM adapter 发送的 "tool_call" chunk 没有被处理，
            #   导致前端看不到工具调用开始，整个流程卡住直到超时。
            # 修复方案: 将 "tool_call" 转换为 "tool_start" 事件发送给前端。
            #   tool_call 是流式增量数据，只在首次收到时发送 tool_start。
            # 影响范围: stream_bridge._handle_chunk
            # 修复日期: 2026-06-01
            _tool_calls = chunk.get("tool_calls", [])
            if _tool_calls:
                await self._close_thinking_if_active(None)
                for _tc in _tool_calls:
                    _tc_idx = getattr(_tc, 'index', 0)
                    _tc_id = getattr(_tc, 'id', None) or f"tc_{_tc_idx}"
                    # 避免重复发送同一 tool_call 的 tool_start
                    if _tc_id not in self._sent_tool_starts:
                        self._sent_tool_starts.add(_tc_id)
                        _seq = self._next_part_seq()
                        _tc_name = ""
                        _tc_args = None
                        if hasattr(_tc, 'function'):
                            _tc_name = getattr(_tc.function, 'name', '') or ""
                            _tc_args = getattr(_tc.function, 'arguments', None)
                        logger.info(
                            "tool_call → tool_start: tool=%s call_id=%s seq=%d pipeline=%s",
                            _tc_name or "unknown", _tc_id, _seq, self.pipeline_id[:12],
                        )
                        await self._send_event(self._make_event("tool_start", {
                            "tool_name": _tc_name or "unknown",
                            "args": _tc_args,
                            "call_id": _tc_id,
                            "sequence": _seq,
                        }))
                        # 收集到 _collected_parts
                        self._collected_parts.append({
                            "type": "tool_call", "callId": _tc_id,
                            "name": _tc_name or "unknown", "args": _tc_args,
                            "state": "calling", "sequence": _seq,
                        })

        elif chunk_type == "tool_start":
            await self._close_thinking_if_active(None)
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
            # 收集到 _collected_parts
            self._collected_parts.append({
                "type": "tool_call", "callId": _call_id,
                "name": chunk.get("tool_name", "unknown"),
                "args": chunk.get("args"),
                "state": "calling", "sequence": _seq,
            })

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
                # FIXUP: 补上 _collected_parts 中缺失的 part
                self._collected_parts.append({
                    "type": "tool_call", "callId": _result_call_id,
                    "name": chunk.get("tool_name", "unknown"),
                    "args": None,
                    "state": "calling", "sequence": _fixup_seq,
                })
            await self._send_event(self._make_event("tool_result", {
                "tool_name": chunk.get("tool_name", "unknown"),
                "success": chunk.get("success", True),
                "result": chunk.get("result"),
                "duration_ms": chunk.get("duration_ms"),
                "call_id": chunk.get("call_id"),
            }))
            # 更新 _collected_parts 中对应 tool_call 的 state/result/name/args
            _result_call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            _result_tool_name = chunk.get("tool_name")
            for p in reversed(self._collected_parts):
                if p.get("type") == "tool_call" and p.get("callId") == _result_call_id:
                    p["state"] = "done" if chunk.get("success", True) else "error"
                    p["result"] = chunk.get("result")
                    p["durationMs"] = chunk.get("duration_ms")
                    # 流式首个 delta 可能不带 function.name，tool_start 存的 name 是 "unknown"
                    # tool_result 带真实名称时回填
                    if _result_tool_name and _result_tool_name != "unknown" and p.get("name") in ("unknown", "", None):
                        p["name"] = _result_tool_name
                    # args 也可能不完整，用 result 事件携带的 args 回填
                    _result_args = chunk.get("args")
                    if _result_args and not p.get("args"):
                        p["args"] = _result_args
                    break
            self._accumulated_content = []

        elif chunk_type == "iteration":
            # 迭代开始时关闭旧的 thinking
            await self._close_thinking_if_active(None)
            await self._send_event(self._make_event("iteration", {
                "iteration": chunk.get("iteration", 0),
                "max_iterations": chunk.get("max_iterations", 0),
            }))

        elif chunk_type == "notification":
            # 系统通知：缓冲到 _pending_notifications，drain_loop 退出时统一刷出
            _notif_content = chunk.get("content", "")
            _notif_seq = self._get_next_sequence()
            self._pending_notifications.append({
                "content": _notif_content,
                "level": chunk.get("level", "info"),
                "notificationType": chunk.get("notificationType", ""),
                "notification_id": f"sys_{self.pipeline_id[:8]}_{_notif_seq}",
            })
            logger.debug(
                "drain_loop: notification buffered: pipeline=%s count=%d",
                self.pipeline_id[:12], len(self._pending_notifications),
            )

    async def drain_loop(
        self,
        engine_task: asyncio.Task | None,
        *,
        heartbeat_callback: Callable[[], Coroutine[Any, Any, None]] | None = None,
        heartbeat_interval: float = 5.0,
        call_timeout: float | None = None,
    ) -> dict:
        """异步消费队列，格式化事件，通过 sink 发送到前端。

        核心消费循环：从内部队列取出 chunk → 转换为前端协议事件 → 经 sink 发送。
        支持心跳保活和挂起超时检测。

        Args:
            engine_task: 管道引擎的异步 Task，用于判断管道是否结束
            heartbeat_callback: 可选的心跳回调协程，在 TimeoutError 时调用
            heartbeat_interval: 心跳间隔秒数，默认 5.0
            call_timeout: 可选的 LLM 活动超时秒数

        Returns:
            dict 包含:
            - accumulated_content: str 累积的完整文本
            - thinking_content_parts: list[str] thinking 内容片段
        """
        _drain_id = uuid.uuid4().hex[:8]
        logger.debug(
            "[DRAIN] drain_loop 开始: msg=%s pipeline=%s sink=%s queueLen=%d drain_id=%s",
            self.message_id[:16], self.pipeline_id[:12], type(self.output_sink).__name__,
            self._queue.qsize(), _drain_id,
        )

        try:
            # 刷出 reset_for_new_turn 保留的缓冲通知
            if self._pending_notifications:
                _preserved = self._pending_notifications[:]
                self._pending_notifications = []
                for _notif in _preserved:
                    await self._send_event(self._make_event("system_notification", _notif))

            # stream_start 延迟到第一个实际 chunk 到达时才发送
            # 避免空转 drain_loop 产生无主占位符

            last_keepalive = asyncio.get_event_loop().time()
            # 挂起超时检测需要独立追踪最后活跃时间
            _last_active = asyncio.get_event_loop().time() if call_timeout is not None else 0.0

            # 2. 主循环：消费队列
            _chunk_count = 0
            _last_chunk = None
            _loop_iter = 0
            while True:
                # engine_task 为空且队列已空 → 没有更多数据，正常退出
                _engine_finished = engine_task is not None and engine_task.done()
                _queue_empty = self._queue.empty()
                if engine_task is None and _queue_empty:
                    _last_chunk = None
                    break
                if _engine_finished and _queue_empty:
                    break
                _loop_iter += 1
                if _loop_iter <= 5 or _loop_iter % 50 == 0:
                    logger.debug(
                        "[DRAIN] drain_loop while 迭代 #%d: pipeline=%s engine_task_done=%s queueLen=%d",
                        _loop_iter, self.pipeline_id[:12],
                        engine_task.done() if engine_task is not None else "N/A", self._queue.qsize(),
                    )
                # BUG-FIX-fix_20260524_ws_push_fail_frontend_stuck:
                # 前端断连时 sink 推送失败是正常现象，后端继续执行不干预。
                try:
                    chunk = await asyncio.wait_for(self._queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    now = asyncio.get_event_loop().time()

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

                    # 超时检测：超时后直接 return
                    if call_timeout is not None:
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
                                    "message_persisted": False,
                                    "final_sequence": 0,
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
                    _last_chunk = None
                    break

                _last_chunk = chunk

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
                    _notif_seq = self._get_next_sequence()
                    self._pending_notifications.append({
                        "content": _notif_content,
                        "level": chunk.get("level", "info"),
                        "notificationType": chunk.get("notificationType", ""),
                        "notification_id": f"sys_{self.pipeline_id[:8]}_{_notif_seq}",
                    })
                    logger.debug(
                        "drain_loop: system_notification buffered: pipeline=%s count=%d",
                        self.pipeline_id[:12], len(self._pending_notifications),
                    )
                    continue

                if _chunk_type in ("tool_start", "tool_result"):
                    logger.debug(
                        "drain: type=%s tool=%s pipeline=%s",
                        _chunk_type, chunk.get('tool_name'), self.pipeline_id[:12],
                    )

                _chunk_count += 1
                logger.debug(
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

            _exit_reason = "unknown"
            if engine_task is not None and engine_task.done():
                _exit_reason = "engine_task_done"
            elif _last_chunk is None:
                _exit_reason = "sentinel_None"

            full_content = "".join(self._accumulated_content)

            # 没有任何内容产生 → 空 drain_loop（如 ensure_bridge 提前启动），
            # 不发送持久化和 stream_end，避免前端看到空气泡
            if _chunk_count == 0 and not full_content:
                logger.warning(
                    "[DRAIN] drain_loop 退出（无内容，不发 stream_end）: reason=%s msg=%s pipeline=%s",
                    _exit_reason, self.message_id[:12], self.pipeline_id[:12],
                )
                return {
                    "accumulated_content": "",
                    "thinking_content_parts": [],
                    "timed_out": False,
                }

            _final_seq = self._get_next_sequence()
            # 构建最终 parts[]，将 streaming/calling 状态转为 done
            _final_parts = [
                {**p, "state": "done"}
                if p.get("state") in ("streaming", "calling") else p
                for p in self._collected_parts
            ]
            await self.send_new_message(full_content, sequence=_final_seq, parts=_final_parts)

            logger.warning(
                "[DRAIN] drain_loop 退出: reason=%s msg=%s pipeline=%s contentLen=%d chunks=%d queueLen=%d final_seq=%d partsLen=%d",
                _exit_reason, self.message_id[:12], self.pipeline_id[:12],
                len(full_content), _chunk_count, self._queue.qsize(), _final_seq, len(_final_parts),
            )
            await self._send_event(self._make_event("stream_end", {
                "full_content": full_content,
                "parts": _final_parts,
                "message_persisted": True,
                "final_sequence": _final_seq,
            }))

            # 刷出缓冲的系统通知
            if self._pending_notifications:
                _notif_count = len(self._pending_notifications)
                for _notif_idx, _notif in enumerate(self._pending_notifications):
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
        except asyncio.CancelledError:
            logger.warning(
                "[DRAIN-CANCEL] drain_loop 被 cancel: pipeline=%s drain_id=%s msg=%s stream_started=%s chunks=%d",
                self.pipeline_id[:12], _drain_id, self.message_id[:12],
                self._stream_started, _chunk_count if '_chunk_count' in dir() else -1,
            )
            if self._stream_started:
                try:
                    await self._close_thinking_if_active(None)
                    _fallback_content = "".join(self._accumulated_content)
                    await self._send_event(self._make_event("stream_end", {
                        "full_content": _fallback_content,
                        "cancelled": True,
                        "message_persisted": False,
                        "final_sequence": 0,
                    }))
                except Exception:
                    pass
            raise
        except Exception as _dl_err:
            # BUG-FIX-fix_20260529_frontend_stuck:
            # drain_loop 异常退出时补发 stream_end，防止前端永远卡在"思考中"
            import traceback as _tb
            logger.error(
                "drain_loop 异常退出: pipeline=%s error=%s error_type=%s stack:\n%s",
                self.pipeline_id[:12], _dl_err, type(_dl_err).__name__,
                _tb.format_exc(),
            )
            if self._stream_started:
                try:
                    await self._close_thinking_if_active(None)
                    _fallback_content = "".join(self._accumulated_content)
                    await self._send_event(self._make_event("stream_end", {
                        "full_content": _fallback_content,
                        "error": True,
                        "message_persisted": False,
                        "final_sequence": 0,
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

    async def send_new_message(self, content: str, sequence: int = 1,
                               parts: list[dict] | None = None) -> None:
        """发送 new_message 最终消息，包含完整的助手消息数据。"""
        effective_content = content
        if not effective_content:
            accumulated = "".join(self._accumulated_content)
            if accumulated:
                effective_content = accumulated

        _event_data: dict = {
            "id": self.message_id,
            "role": "assistant",
            "content": effective_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
        }
        if parts is not None:
            _event_data["parts"] = parts
        await self._send_event(self._make_event("new_message", _event_data))


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
