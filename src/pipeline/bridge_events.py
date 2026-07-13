"""流式事件格式化处理。

处理 LLM 输出的各种 chunk 类型（text/thinking/tool/tool_result 等），
转换为前端 WebSocket 协议事件格式。

Phase 1 改造：删除 _accumulated_content / _collected_parts / _thinking_content_parts 的累加，
bridge 不再独立累加内容，state.raw_result 是唯一数据源。
_handle_chunk 只负责"格式化 + 推送"，数据持久化由 state → TrackPlugin 负责。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class BridgeEventsMixin:
    """事件格式化 Mixin，混入 BridgeCore 使用。

    提供 _close_thinking_if_active、_handle_chunk 等事件转换方法，
    将内部 chunk 格式化为前端协议事件。
    Phase 1 改造：_handle_chunk 不再累加内容，只做格式化 + 推送。
    """

    # 声明由 BridgeCore 提供的属性（类型检查用）
    _thinking_active: bool
    _stream_started: bool
    _sent_tool_starts: set[str]
    _llm_seen_call_ids: set[str]
    pipeline_id: str
    message_id: str

    # _make_event, _send_event, _next_part_seq 由 BridgeCore 提供

    async def _close_thinking_if_active(self, duration_ms: Any = None) -> None:
        """如果 thinking 处于活跃状态，发送 thinking_end 事件关闭。"""
        if self._thinking_active:
            self._thinking_active = False
            await self._send_event(
                self._make_event(
                    "thinking_end",
                    {
                        "duration_ms": duration_ms,
                    },
                )
            )
            # 思考块结束：重置块追踪，使后续（正文或新思考）开新 sequence 块。
            self._reset_current_block()

    async def _handle_chunk(self, chunk: dict) -> None:  # noqa: PLR0912
        """处理单个 chunk 事件，转换为前端协议格式并发送。

        Phase 1 改造：不再累加到 _accumulated_content / _collected_parts，
        只做格式化 + 推送。完整内容从 state.raw_result 由 emit_finish 推送。

        Args:
            chunk: 包含 type 和 content 等字段的管道事件字典
        """
        chunk_type = chunk.get("type", "text")
        content = chunk.get("content", "")

        if chunk_type == "text" and content:
            # 推送 stream_chunk（不累加，完整内容由 emit_finish 从 state 推送）
            # sequence 按 part 块分配：同一段正文的连续 chunk 共享一个 sequence，
            # 避免长输出把计数器推高导致与后续/其它 part 的 sequence 交错（见 _seq_for_block）。
            await self._send_event(
                self._make_event(
                    "stream_chunk",
                    {
                        "content": content,
                        "sequence": self._seq_for_block("text"),
                    },
                )
            )

        elif chunk_type == "thinking" and content:
            # 同一段思考的连续 chunk 共享一个 sequence（块级）。
            if not self._thinking_active:
                self._thinking_active = True
                await self._send_event(
                    self._make_event(
                        "thinking_start",
                        {
                            "sequence": self._seq_for_block("thinking"),
                        },
                    )
                )
            await self._send_event(
                self._make_event(
                    "thinking_chunk",
                    {
                        "content": content,
                        "sequence": self._current_block_seq,
                        "step_type": chunk.get("step_type", ""),
                    },
                )
            )

        elif chunk_type == "thinking_end":
            await self._close_thinking_if_active(chunk.get("duration_ms"))

        elif chunk_type == "tool_call":
            _tool_calls = chunk.get("tool_calls", [])
            if _tool_calls:
                await self._close_thinking_if_active(None)
                for _tc in _tool_calls:
                    _tc_id = getattr(_tc, "id", None)
                    if _tc_id:
                        self._llm_seen_call_ids.add(_tc_id)

        elif chunk_type == "tool_start":
            await self._close_thinking_if_active(None)
            _call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
            _tool_name = chunk.get("tool_name", "unknown")
            if _call_id in self._sent_tool_starts:
                logger.debug(
                    "tool_start skipped (dedup): tool=%s call_id=%s pipeline=%s",
                    _tool_name,
                    _call_id,
                    self.pipeline_id[:12],
                )
                return
            self._sent_tool_starts.add(_call_id)
            _seq = self._next_part_seq()
            self._reset_current_block()
            logger.debug(
                "tool_start: tool=%s call_id=%s seq=%d pipeline=%s",
                _tool_name,
                _call_id,
                _seq,
                self.pipeline_id[:12],
            )
            await self._send_event(
                self._make_event(
                    "tool_start",
                    {
                        "tool_name": _tool_name,
                        "args": chunk.get("args"),
                        "call_id": chunk.get("call_id"),
                        "sequence": _seq,
                    },
                )
            )

        elif chunk_type == "tool_result":
            await self._handle_tool_result(chunk)

        elif chunk_type == "tool_multimedia_result":
            await self._send_event(
                self._make_event(
                    "tool_multimedia_result",
                    {
                        "count": chunk.get("count", 0),
                        "multimedia": chunk.get("multimedia", []),
                        "sequence": self._next_part_seq(),
                    },
                )
            )
            self._reset_current_block()

        elif chunk_type == "iteration":
            await self._close_thinking_if_active(None)
            await self._send_event(
                self._make_event(
                    "iteration",
                    {
                        "iteration": chunk.get("iteration", 0),
                        "max_iterations": chunk.get("max_iterations", 0),
                    },
                )
            )

        elif chunk_type == "notification":
            await self._send_event(
                self._make_event(
                    "system_notification",
                    {
                        "content": chunk.get("content", ""),
                        "level": chunk.get("level", "info"),
                        "notificationType": chunk.get("notificationType", ""),
                        "notification_id": chunk.get("notification_id", ""),
                        "sequence": chunk.get("sequence", 0),
                    },
                )
            )
            self._reset_current_block()

    async def _handle_tool_result(self, chunk: dict) -> None:
        """处理 tool_result chunk，自动补发缺失的 tool_start。"""
        _result_call_id = chunk.get("call_id") or chunk.get("tool_name", "unknown")
        if _result_call_id not in self._sent_tool_starts and _result_call_id not in self._llm_seen_call_ids:
            logger.info(
                "FIXUP: tool_result without tool_start: tool=%s pipeline=%s",
                chunk.get("tool_name"),
                self.pipeline_id[:12],
            )
            self._sent_tool_starts.add(_result_call_id)
            await self._send_event(
                self._make_event(
                    "tool_start",
                    {
                        "tool_name": chunk.get("tool_name", "unknown"),
                        "args": None,
                        "call_id": chunk.get("call_id"),
                        "sequence": self._next_part_seq(),
                    },
                )
            )
            self._reset_current_block()
        await self._send_event(
            self._make_event(
                "tool_result",
                {
                    "tool_name": chunk.get("tool_name", "unknown"),
                    "success": chunk.get("success", True),
                    "result": chunk.get("result"),
                    "duration_ms": chunk.get("duration_ms"),
                    "call_id": chunk.get("call_id"),
                },
            )
        )
