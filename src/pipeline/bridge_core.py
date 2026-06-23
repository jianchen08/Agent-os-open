"""流式桥接器核心（无状态转发器）。

engine 主动 emit，bridge 只负责格式化和推送。
state.raw_result 是唯一数据源，推送和持久化都从这里取。

公共 emit 接口（由 engine 调用）：
- emit_start(state): 生成 hex ID + 发 stream_start + 写 state.preset_ai_record_id
- emit_chunk(chunk): 包装 chunk + 推送（不累加）
- emit_finish(state): 从 state 取内容发 new_message + stream_end
- emit_suspend(state): 发 state_change + stream_end
- emit_error(exc): 发 stream_error
- emit_notification(content): 直接推送系统通知（替代 enqueue_notification）
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


class BridgeCore:
    """流式桥接器核心状态（无状态转发器）。

    engine 主动调 emit_*，bridge 从 state 取内容推送，不独立累加。
    删除了 drain_loop / queue / _accumulated_content / _collected_parts。
    """

    # 由子模块引用的类型注解
    _output_sink: Any  # IOutputSink
    pipeline_id: str
    message_id: str

    def _init_core_state(
        self,
        pipeline_id: str,
        output_sink: Any,
        message_id: str | None = None,
    ) -> None:
        """初始化核心状态（由 __init__ 调用）。

        Args:
            pipeline_id: 管道 ID
            output_sink: 输出目标
            message_id: 消息 ID，不传则自动生成 hex 格式
        """
        self.pipeline_id = pipeline_id
        self.output_sink = output_sink
        # hex 格式 message_id（无 msg_ 前缀），与前端/API record_id 全程一致
        self.message_id = message_id or uuid.uuid4().hex[:12]
        self._container_task_id: str = ""
        self._entry: Any | None = None

        # 绑定日志上下文，使后续日志自动携带 pipeline_id / task_id
        from src.core.logging import LogContext  # noqa: PLC0415
        _ctx: dict[str, str] = {"pipeline_id": pipeline_id}
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            self._entry = get_engine_registry().get(pipeline_id)
            if self._entry and hasattr(self._entry, "tags"):
                self._container_task_id = self._entry.tags.get("task_id", "")
                if self._container_task_id:
                    _ctx["task_id"] = self._container_task_id
                _thread_id = self._entry.tags.get("session_id", "")
                if _thread_id:
                    _ctx["session_id"] = _thread_id
        except Exception:
            logger.debug(
                "BridgeCore: 获取 PipelineEntry 失败 pipeline=%s",
                pipeline_id[:12], exc_info=True,
            )

        # 绑定日志上下文（contextvars，async 安全）
        LogContext.bind(**_ctx)

        # 状态追踪（仅当前 turn，不跨 turn 累加内容）
        self._stream_started: bool = False
        self._thinking_active: bool = False
        self._sent_tool_starts: set[str] = set()
        self._llm_seen_call_ids: set[str] = set()
        self._part_seq: int = 0
        self._emit_start_time: float = 0.0

    # ------------------------------------------------------------------
    # sequence（保留，用于 system_notification 等消息级序号）
    # ------------------------------------------------------------------

    def _get_next_sequence(self) -> int:
        """从 PipelineEntry 共享计数器获取下一个 sequence。"""
        if getattr(self, '_entry', None) is not None:
            return self._entry.next_sequence()
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            entry = get_engine_registry().get(self.pipeline_id)
            if entry is not None:
                self._entry = entry
                return entry.next_sequence()
        except Exception:
            logger.debug(
                "BridgeCore: 获取 sequence 失败 pipeline=%s",
                self.pipeline_id[:12], exc_info=True,
            )
        return 0

    def _next_part_seq(self) -> int:
        """Part 级 sequence，本地递增，仅用于前端 parts 排序。"""
        self._part_seq += 1
        return self._part_seq

    # ------------------------------------------------------------------
    # 事件构造与发送
    # ------------------------------------------------------------------

    def _make_event(self, event_type: str, data: dict) -> dict:
        """构造事件字典，自动注入信封字段和 pipeline_id、message_id、container_task_id。

        按 WebSocket 协议要求（需求文档 §2.1），每个事件信封必须包含：
        - type: 事件类型
        - data: 事件数据
        - source_type: 消息来源类型（system/agent/user/tool）
        - source_id: 来源标识
        - timestamp: ISO 8601 时间戳
        """
        data.setdefault("pipeline_id", self.pipeline_id)
        data.setdefault("message_id", self.message_id)
        if self._container_task_id:
            data.setdefault("container_task_id", self._container_task_id)
        return {
            "type": event_type,
            "data": data,
            "source_type": "system",
            "source_id": self.pipeline_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def _send_event_internal(self, event: dict) -> bool:
        """内部事件发送实现（失败隔离：只 log warning，不抛异常）。"""
        try:
            success = await self.output_sink.send_event(event)
            if not success:
                logger.warning(
                    "[Bridge] 推送返回 False: type=%s sink=%s pipeline=%s",
                    event.get("type", "unknown"),
                    getattr(self.output_sink, 'sink_id', '?'),
                    self.pipeline_id[:12],
                )
            return success
        except Exception as e:
            logger.warning(
                "[Bridge] 推送异常: type=%s error=%s pipeline=%s",
                event.get("type", "unknown"), e, self.pipeline_id[:12],
            )
            return False

    # 保持旧名作为内部别名
    _send_event = _send_event_internal

    async def send_event(self, event: dict) -> bool:
        """通过 output_sink 发送事件（公共接口）。

        外部模块应使用此方法而非访问 _send_event 私有方法。

        Args:
            event: 要发送的事件字典

        Returns:
            发送成功返回 True，失败返回 False
        """
        return await self._send_event_internal(event)

    # ------------------------------------------------------------------
    # emit 接口（engine 主动调用）
    # ------------------------------------------------------------------

    def _start_new_turn(self, state: dict[str, Any] | None = None) -> None:
        """同步部分：为新 turn 生成 hex ID、重置状态、写入 state.preset_ai_record_id。

        在 emit_start 前同步调用，确保 message_id 立即可用（不依赖 async 完成）。
        """
        self.message_id = uuid.uuid4().hex[:12]
        self._stream_started = True
        self._thinking_active = False
        self._sent_tool_starts = set()
        self._llm_seen_call_ids = set()
        self._part_seq = 0
        self._emit_start_time = time.monotonic()
        # 写入 state，供 TrackPlugin 用作 record_id（与 stream_start 下发的 ID 一致）
        if state is not None:
            state["preset_ai_record_id"] = self.message_id

    async def emit_start(self, state: dict[str, Any] | None = None) -> None:
        """生成 hex ID + 发 stream_start。

        Args:
            state: 管道状态字典（可选），用于写入 preset_ai_record_id
        """
        if not self._stream_started:
            self._start_new_turn(state)
        logger.info(
            "[Bridge] emit_start: msg=%s pipeline=%s sink=%s",
            self.message_id[:12], self.pipeline_id[:12],
            getattr(self.output_sink, 'sink_id', '?'),
        )
        try:
            await self._send_event(self._make_event("stream_start", {
                "message_id": self.message_id,
                "pipeline_id": self.pipeline_id,
                "_threadId": getattr(self.output_sink, '_thread_id', None),
            }))
        except Exception as e:
            logger.warning(
                "[Bridge] emit_start 推送失败: msg=%s error=%s",
                self.message_id[:12], e,
            )

    async def emit_chunk(self, chunk: dict) -> None:
        """包装 chunk + 推送（不累加，数据源在 state）。

        Args:
            chunk: 包含 type 和 content 等字段的管道事件字典
        """
        # 正常流程：emit_start 一定在 chunk 之前。如果 _stream_started=False，
        # 说明上游时序有 bug，直接丢弃 chunk 并告警，不做任何兜底掩盖问题。
        if not self._stream_started:
            chunk_type = chunk.get("type", "?") if isinstance(chunk, dict) else "?"
            logger.warning(
                "[Bridge] chunk 丢弃：_stream_started=False（emit_start 未调用或已结束），"
                "type=%s msg=%s pipeline=%s",
                chunk_type, self.message_id[:12], self.pipeline_id[:12],
            )
            return
        try:
            await self._handle_chunk(chunk)
        except Exception as e:
            chunk_type = chunk.get("type", "?") if isinstance(chunk, dict) else "?"
            logger.warning(
                "[Bridge] emit_chunk 失败: type=%s error=%s pipeline=%s",
                chunk_type, e, self.pipeline_id[:12],
            )

    async def emit_finish(self, state: dict[str, Any]) -> None:
        """从 state 取内容发 new_message + stream_end。

        state.raw_result 是唯一数据源，推送和持久化都从这里取。

        Args:
            state: 管道状态字典
        """
        await self._close_thinking_if_active(None)
        full_content = state.get("raw_result") or ""
        parts = self._build_parts_from_state(state)
        elapsed_ms = int(
            (time.monotonic() - self._emit_start_time) * 1000
        ) if self._emit_start_time else 0
        logger.info(
            "[Bridge] emit_finish: msg=%s pipeline=%s content_len=%d parts=%d elapsed_ms=%d",
            self.message_id[:12], self.pipeline_id[:12],
            len(full_content), len(parts), elapsed_ms,
        )
        try:
            final_seq = self._get_next_sequence()
            # 发 new_message（完整助手消息，供前端直接渲染）
            # 空内容 + 空 parts 时跳过 new_message，避免空气泡（只发 stream_end）
            if full_content or parts:
                await self._send_event(self._make_event("new_message", {
                    "id": self.message_id,
                    "role": "assistant",
                    "content": full_content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sequence": final_seq,
                    "parts": parts,
                }))
            else:
                logger.info(
                    "[Bridge] emit_finish: 内容为空，跳过 new_message 只发 stream_end: msg=%s",
                    self.message_id[:12],
                )
            # 发 stream_end（标记流式完成）
            await self._send_event(self._make_event("stream_end", {
                "full_content": full_content,
                "parts": parts,
                "message_persisted": True,
                "final_sequence": final_seq,
            }))
        except Exception as e:
            logger.warning(
                "[Bridge] emit_finish 推送失败: msg=%s error=%s",
                self.message_id[:12], e,
            )
        finally:
            self._stream_started = False

    async def emit_suspend(self, state: dict[str, Any]) -> None:
        """发 state_change + stream_end（挂起，本轮流式完成）。

        Args:
            state: 管道状态字典
        """
        await self._close_thinking_if_active(None)
        logger.info(
            "[Bridge] emit_suspend: msg=%s pipeline=%s",
            self.message_id[:12], self.pipeline_id[:12],
        )
        full_content = state.get("raw_result") or ""
        parts = self._build_parts_from_state(state)
        try:
            await self._send_event(self._make_event("state_change", {
                "status": "suspended",
                "pipeline_id": self.pipeline_id,
                "thread_id": getattr(self.output_sink, '_thread_id', '') or "",
            }))
            final_seq = self._get_next_sequence()
            if full_content or parts:
                await self._send_event(self._make_event("new_message", {
                    "id": self.message_id,
                    "role": "assistant",
                    "content": full_content,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sequence": final_seq,
                    "parts": parts,
                }))
            await self._send_event(self._make_event("stream_end", {
                "full_content": full_content,
                "parts": parts,
                "message_persisted": True,
                "final_sequence": final_seq,
            }))
        except Exception as e:
            logger.warning(
                "[Bridge] emit_suspend 推送失败: msg=%s error=%s",
                self.message_id[:12], e,
            )
        finally:
            self._stream_started = False

    async def emit_error(self, exc: BaseException) -> None:
        """发 stream_error。

        Args:
            exc: 异常对象
        """
        await self._close_thinking_if_active(None)
        error_msg = str(exc)
        logger.error(
            "[Bridge] emit_error: msg=%s pipeline=%s error_type=%s error=%s",
            self.message_id[:12], self.pipeline_id[:12],
            type(exc).__name__, error_msg[:200],
        )
        try:
            await self._send_event(self._make_event("stream_error", {
                "error": f"管道执行失败: {error_msg}",
                "message_persisted": False,
            }))
        except Exception as e:
            logger.warning(
                "[Bridge] emit_error 推送失败: msg=%s error=%s",
                self.message_id[:12], e,
            )
        finally:
            self._stream_started = False

    async def emit_notification(
        self,
        content: str,
        *,
        source: str = "system",
        level: str = "info",
    ) -> int:
        """直接推送系统通知（替代原 enqueue_notification，不再走队列）。

        Args:
            content: 通知内容
            source: 通知来源
            level: 通知级别

        Returns:
            通知的 sequence 值，-1 表示内容为空
        """
        if not content or not content.strip():
            return -1
        seq = self._get_next_sequence()
        logger.info(
            "[Bridge] emit_notification: seq=%d source=%s pipeline=%s content=%.50s",
            seq, source, self.pipeline_id[:12], content[:50],
        )
        try:
            await self._send_event(self._make_event("system_notification", {
                "content": content.strip(),
                "source": source,
                "level": level,
                "notificationType": f"{source}_notification",
                "notification_id": f"sys_{self.pipeline_id[:8]}_{seq}",
                "sequence": seq,
            }))
        except Exception as e:
            logger.warning(
                "[Bridge] emit_notification 推送失败: error=%s", e,
            )
        return seq

    # ------------------------------------------------------------------
    # parts 重建（从 state，单一数据源）
    # ------------------------------------------------------------------

    def _build_parts_from_state(self, state: dict[str, Any]) -> list[dict]:
        """从 state 重建 parts[]（Phase 2 完整实现，Phase 1 基础版）。

        数据来源：
        - state.raw_thinking → thinking part
        - state.raw_tool_calls + state.tool_results → tool_call parts
        - state.raw_result → text part
        """
        parts: list[dict] = []
        thinking = state.get("raw_thinking")
        if thinking:
            parts.append({
                "type": "thinking", "content": thinking,
                "state": "done", "sequence": self._next_part_seq(),
            })
        tool_calls = state.get("raw_tool_calls") or []
        tool_results = state.get("tool_results") or []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id", "")
            name = tc.get("name", "unknown")
            args = tc.get("args")
            result = None
            success = True
            for tr in tool_results:
                if not isinstance(tr, dict):
                    continue
                if tr.get("call_id") == call_id or tr.get("tool_name") == name:
                    result = tr.get("data") or tr.get("result")
                    success = tr.get("success", True)
                    break
            parts.append({
                "type": "tool_call", "callId": call_id, "name": name,
                "args": args,
                "state": "done" if success else "error",
                "result": result,
                "sequence": self._next_part_seq(),
            })
        raw_result = state.get("raw_result")
        if raw_result:
            parts.append({
                "type": "text", "content": raw_result,
                "state": "done", "sequence": self._next_part_seq(),
            })
        return parts

    # ------------------------------------------------------------------
    # 兼容旧接口（保留过渡，内部转为新行为）
    # ------------------------------------------------------------------

    def reset_for_new_turn(self, message_id: str | None = None) -> None:
        """重置内部状态，为新的一轮对话做准备。

        Args:
            message_id: 新的消息 ID（hex 格式），不传则自动生成
        """
        self.message_id = message_id or uuid.uuid4().hex[:12]
        self._stream_started = False
        self._thinking_active = False
        self._sent_tool_starts = set()
        self._llm_seen_call_ids = set()
        self._part_seq = 0

    def stop(self) -> None:
        """[DEPRECATED] 停止 drain_loop。

        Phase 1 改造：drain_loop 已删除，此方法为空实现，仅为兼容旧调用链。
        """
        pass

    @property
    def on_chunk(self) -> Any:
        """[DEPRECATED] bridge 不再提供 on_chunk 回调。

        Phase 1 改造：on_chunk 由 engine 内部 _on_chunk_adapter 处理。
        保留此 property 仅为防止旧代码 AttributeError。
        """
        return None

    async def send_new_message(
        self,
        content: str,
        sequence: int = 1,
        parts: list[dict] | None = None,
    ) -> None:
        """[DEPRECATED] 发送 new_message 最终消息。

        保留用于过渡兼容，新代码应使用 emit_finish(state)。
        """
        _event_data: dict = {
            "id": self.message_id,
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
        }
        if parts is not None:
            _event_data["parts"] = parts
        await self._send_event(self._make_event("new_message", _event_data))
