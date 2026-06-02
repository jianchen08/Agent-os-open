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
# MultiChannelSink — 多通道输出分发
# ---------------------------------------------------------------------------

class MultiChannelSink:
    """多渠道输出分发器。将 bridge 产出的内部事件分发给所有注册的通道。

    每个通道实现 IOutputSink 协议，由 MultiChannelSink 统一管理：
    - 单个通道 dead 不影响其他通道
    - 全部通道 dead 才返回 is_dead=True
    - 新增通道只需 register，不改 bridge 核心逻辑
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

    @property
    def is_dead(self) -> bool:
        """全部通道都 dead 才返回 True。"""
        if not self._channels:
            return True
        return all(getattr(s, 'is_dead', False) for s in self._channels.values())

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

        BUG-FIX-fix_20260530_drain_loop_not_running:
        当发现 drain_task 不在运行时，自动启动新的 drain_loop。
        这是最后一道防线，确保 LLM 生成的 chunks 一定有消费者。

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
        _ctype = chunk.get("type", "?") if isinstance(chunk, dict) else "?"
        if _ctype not in ("iteration", "stream_keepalive"):
            _drain_running = False
            _drain_detail = ""
            _entry_ref = None
            try:
                from pipeline.registry import get_engine_registry
                _e = get_engine_registry().get(self.pipeline_id)
                _entry_ref = _e
                if _e is None:
                    _drain_detail = "no_entry"
                elif _e.drain_task is None:
                    _drain_detail = "task_None"
                elif _e.drain_task.done():
                    _exc = _e.drain_task.exception()
                    _drain_detail = f"task_done(exc={_exc})" if _exc else "task_done"
                else:
                    _drain_running = True
                    _drain_detail = "alive"
            except Exception as ex:
                _drain_detail = f"err:{ex}"
            logger.debug(
                "[CHUNK-TRACE] on_chunk: pipeline=%s msg=%s type=%s queueLen=%d drain=%s(%s)",
                self.pipeline_id[:12], self.message_id[:12],
                _ctype, self._queue.qsize(), _drain_running, _drain_detail,
            )
            # BUG-FIX-fix_20260531_drain_autofix_race:
            # 修复 DRAIN-AUTOFIX 竞态条件：当前端 WebSocket 连接丢失后，
            # TargetedSink 标记 is_dead=True，drain_loop 检测到后退出。
            # 但 on_chunk 的 DRAIN-AUTOFIX 不检查 sink.is_dead，无条件重启
            # drain_loop，导致 drain_loop 反复启动又立即退出的死循环。
            # 修复：sink 已 dead 时不重启 drain_loop，改为静默丢弃 chunk 并清空队列。
            _sink_is_dead = getattr(self.output_sink, 'is_dead', False) is True
            if not _drain_running and _entry_ref is not None and _entry_ref.engine is not None:
                if _sink_is_dead:
                    # sink 已死亡，重启 drain_loop 也只会立即退出，因此不重启
                    logger.debug(
                        "[DRAIN-AUTOFIX] sink 已死亡，跳过重启 drain_loop，静默丢弃 chunk: "
                        "pipeline=%s msg=%s detail=%s",
                        self.pipeline_id[:12], self.message_id[:12], _drain_detail,
                    )
                    # 清空队列避免 chunk 堆积导致内存泄漏
                    self._drain_queue_safe()
                else:
                    logger.warning(
                        "[DRAIN-AUTOFIX] on_chunk 发现 drain_loop 不在运行，自动启动: pipeline=%s msg=%s detail=%s",
                        self.pipeline_id[:12], self.message_id[:12], _drain_detail,
                    )
                    try:
                        from pipeline.message_bus import _start_bg_drain
                        _start_bg_drain(self.pipeline_id, self, _entry_ref.engine, engine_task=_entry_ref.engine_task)
                        logger.warning(
                            "[DRAIN-AUTOFIX] drain_loop 已自动启动: pipeline=%s msg=%s",
                            self.pipeline_id[:12], self.message_id[:12],
                        )
                    except Exception as _af_err:
                        logger.error(
                            "[DRAIN-AUTOFIX] 自动启动 drain_loop 失败: pipeline=%s error=%s",
                            self.pipeline_id[:12], _af_err,
                        )

    def _drain_queue_safe(self) -> None:
        """安全清空内部队列，避免 sink 死亡后 chunk 堆积导致内存泄漏。

        BUG-FIX-fix_20260531_drain_autofix_race:
        当 sink 已死亡且 drain_loop 不再运行时，队列中的 chunk
        没有消费者，会持续堆积造成内存泄漏。此方法在确认 sink 死亡后
        由 on_chunk 的 DRAIN-AUTOFIX 分支调用，循环取出并丢弃
        队列中所有残留 chunk。
        """
        _drained = 0
        while True:
            try:
                self._queue.get_nowait()
                _drained += 1
            except Exception:
                # queue.Empty 或其他异常均停止
                break
        if _drained > 0:
            logger.debug(
                "[DRAIN-AUTOFIX] 已清空队列中 %d 个残留 chunk: pipeline=%s msg=%s",
                _drained, self.pipeline_id[:12], self.message_id[:12],
            )

    def _notify_engine_sink_dead(self) -> None:
        """通知引擎 sink 已死亡，应停止运行。

        BUG-FIX-fix_20260531_sink_dead_stop_engine:
        当前端 WebSocket 连接丢失后，drain_loop 检测到 sink dead 退出，
        但引擎仍在运行浪费 LLM token。此方法通过 Registry 找到引擎，
        设置 ended 标志并唤醒挂起状态，使引擎尽快停止。

        处理两种引擎状态：
        1. 引擎处于挂起状态：设置 _suspended_state["ended"]=True 并唤醒
        2. 引擎正在运行：设置 _should_stop 标志，stop_check 插件会在下次检查时终止
        """
        try:
            from pipeline.registry import get_engine_registry
            _reg = get_engine_registry()
            _entry = _reg.get(self.pipeline_id)
            if _entry is None or _entry.engine is None:
                return
            if hasattr(_entry.engine, 'request_stop'):
                _entry.engine.request_stop()
                logger.warning("[SINK-DEAD] 已通过 request_stop() 通知引擎停止: pipeline=%s", self.pipeline_id[:12])
            else:
                # 兼容旧版无 request_stop 的引擎
                _entry.engine._should_stop = True
        except Exception as _ex:
            logger.error(
                "[SINK-DEAD] 通知引擎停止失败: pipeline=%s error=%s",
                self.pipeline_id[:12], _ex,
            )

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
        self._sent_tool_starts = set()
        self._part_seq = 0
        self._current_msg_seq = 0
        # BUG-FIX-fix_20260531_sink_dead_persist:
        # 如果 output_sink 是 TargetedSink 且已被标记 dead，
        # 重置其 dead 状态和失败计数，给新一轮发送机会。
        # ensure_bridge 在调用 reset_for_new_turn 之前已更新 output_sink，
        # 但引擎内部唤醒路径（_suspend_and_wait）不经过 ensure_bridge，
        # sink dead 状态可能残留。
        _sink = getattr(self, 'output_sink', None)
        if _sink is not None:
            if getattr(_sink, '_is_dead', False):
                _sink._is_dead = False
                _sink._fail_count = 0
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
                _pending_text = "".join(self._accumulated_content)
                if _pending_text:
                    await self._send_event(self._make_event("stream_end", {
                        "full_content": _pending_text,
                    }))
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
        _drain_id = uuid.uuid4().hex[:8]
        logger.warning(
            "[DRAIN] drain_loop 开始: msg=%s pipeline=%s sink=%s queueLen=%d drain_id=%s",
            self.message_id[:16], self.pipeline_id[:12], type(self.output_sink).__name__,
            self._queue.qsize(), _drain_id,
        )

        # BUG-FIX-fix_20260530_queue_event_loop:
        # 防御性检查：如果 Queue 绑定了不同的事件循环，立即重建。
        # 正常情况下 reset_for_new_turn 已处理，此处为双重保险。
        try:
            _current_loop = asyncio.get_running_loop()
            if getattr(self._queue, '_loop', None) is not _current_loop:
                logger.warning(
                    "drain_loop: Queue 绑定了不同事件循环，重建 Queue/Event: pipeline=%s",
                    self.pipeline_id[:12],
                )
                self._queue = asyncio.Queue()
                self._chunk_event = asyncio.Event()
        except RuntimeError:
            pass

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
                logger.debug(
                    "[DRAIN] stream_start 已发送: msg=%s pipeline=%s",
                    self.message_id[:12], self.pipeline_id[:12],
                )
            except Exception as _e:
                logger.error("drain_loop: stream_start 失败: %s", _e, exc_info=True)
                return {"accumulated_content": "", "thinking_content_parts": [], "timed_out": False}

            last_keepalive = asyncio.get_event_loop().time()
            # 挂起超时检测需要独立追踪最后活跃时间
            _last_active = asyncio.get_event_loop().time() if call_timeout is not None else 0.0

            # 2. 主循环：消费队列
            _chunk_count = 0
            _actually_suspended = False
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
                            "message_persisted": False,
                            "final_sequence": 0,
                        }))
                    except Exception:
                        pass
                    # 丢弃缓冲的通知（连接已死，通知无需发送）
                    self._pending_notifications = []
                    # BUG-FIX-fix_20260531_sink_dead_stop_engine:
                    # sink 死亡后通知引擎停止运行，避免 LLM 继续生成无用的 token。
                    # 通过 Registry 找到引擎，设置 ended 标志并唤醒挂起状态。
                    self._notify_engine_sink_dead()
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
                        (engine_task is None or not engine_task.done())
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
                            logger.warning(
                                "[DRAIN] drain_loop 挂起退出: pipeline=%s msg=%s chunks=%d accumulatedLen=%d",
                                self.pipeline_id[:12], self.message_id[:12],
                                _chunk_count, len("".join(self._accumulated_content)),
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
            elif _actually_suspended:
                _exit_reason = "suspended_grace_expired"
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

            # 只有引擎真正完成或队列已空时才持久化 + 发 stream_end
            # 挂起退出时不发——引擎还在运行，只是暂停了
            if _exit_reason not in ("suspended_grace_expired",):
                _final_seq = self._get_next_sequence()
                await self.send_new_message(full_content, sequence=_final_seq)

                logger.warning(
                    "[DRAIN] drain_loop 退出: reason=%s msg=%s pipeline=%s contentLen=%d chunks=%d queueLen=%d final_seq=%d",
                    _exit_reason, self.message_id[:12], self.pipeline_id[:12],
                    len(full_content), _chunk_count, self._queue.qsize(), _final_seq,
                )
                await self._send_event(self._make_event("stream_end", {
                    "full_content": full_content,
                    "message_persisted": True,
                    "final_sequence": _final_seq,
                }))
            else:
                logger.warning(
                    "[DRAIN] drain_loop 挂起退出（引擎未完成，不发 stream_end）: reason=%s msg=%s pipeline=%s contentLen=%d chunks=%d",
                    _exit_reason, self.message_id[:12], self.pipeline_id[:12],
                    len(full_content), _chunk_count,
                )

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

    async def send_new_message(self, content: str, sequence: int = 1) -> None:
        """发送 new_message 最终消息，包含完整的助手消息数据。"""
        effective_content = content
        if not effective_content:
            accumulated = "".join(self._accumulated_content)
            if accumulated:
                effective_content = accumulated

        await self._send_event(self._make_event("new_message", {
            "id": self.message_id,
            "role": "assistant",
            "content": effective_content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
        }))


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
