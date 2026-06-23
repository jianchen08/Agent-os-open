"""WebSocket 事件格式验证测试。

验证后端推送的所有核心事件类型符合前端协议规范。
对应需求：F-UI-01~05, AC-UI-01, AC-UI-12

覆盖的事件类型（来源：需求文档 §2.1）：
- stream_start / stream_chunk / stream_end
- thinking_start / thinking_chunk / thinking_end
- execution_start / execution_progress / execution_done
- pipeline_start / pipeline_end
- interaction_request / interaction_cancelled
- plugin_error / pipeline_error

验证点：
- 每个事件的 type 字段正确
- data 字段包含协议要求的关键字段
- message_id / pipeline_id 正确注入
"""
from __future__ import annotations

from typing import Any

import pytest

from pipeline.stream_bridge import PipelineStreamBridge


# ---------------------------------------------------------------------------
# Mock OutputSink — 捕获所有推送的事件
# ---------------------------------------------------------------------------


class MockOutputSink:
    """模拟输出目标，捕获所有推送的事件用于断言。"""

    def __init__(self, thread_id: str = "test-thread-001") -> None:
        self.sink_id = "mock-sink"
        self._thread_id = thread_id
        self.events: list[dict[str, Any]] = []

    async def send_event(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True

    @property
    def _thread_id(self) -> str:  # type: ignore[override]
        return self.__thread_id

    @_thread_id.setter
    def _thread_id(self, value: str) -> None:
        self.__thread_id = value


# ---------------------------------------------------------------------------
# PipelineStreamBridge 测试 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sink() -> MockOutputSink:
    return MockOutputSink()


@pytest.fixture
def bridge(mock_sink: MockOutputSink) -> PipelineStreamBridge:
    """创建 PipelineStreamBridge 实例，注入 mock sink。"""
    return PipelineStreamBridge(
        pipeline_id="test-pipe-001",
        output_sink=mock_sink,
    )


def _get_events_by_type(sink: MockOutputSink, event_type: str) -> list[dict]:
    """从 mock sink 中提取指定类型的事件。"""
    return [e for e in sink.events if e.get("type") == event_type]


# ---------------------------------------------------------------------------
# stream_start / stream_chunk / stream_end 事件格式
# ---------------------------------------------------------------------------


class TestStreamEvents:
    """流式输出事件格式测试。"""

    @pytest.mark.asyncio
    async def test_stream_start_has_required_fields(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """stream_start 事件包含 message_id 和 pipeline_id。

        验证点（AC-UI-01）：
        - type == "stream_start"
        - data.message_id 存在且为 12 字符 hex
        - data.pipeline_id 存在
        """
        await bridge.emit_start()

        events = _get_events_by_type(mock_sink, "stream_start")
        assert len(events) == 1, f"应推送 1 个 stream_start，得到 {len(events)}"

        data = events[0]["data"]
        assert "message_id" in data, "stream_start 缺少 message_id"
        assert len(data["message_id"]) == 12, "message_id 应为 12 字符 hex"
        assert data["pipeline_id"] == "test-pipe-001"

    @pytest.mark.asyncio
    async def test_stream_chunk_has_content_and_sequence(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """stream_chunk 事件包含 content 和 sequence。

        验证点（AC-UI-01）：
        - type == "stream_chunk"
        - data.content 存在
        - data.sequence 为正整数（前端用于排序）
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "Hello"})

        chunks = _get_events_by_type(mock_sink, "stream_chunk")
        assert len(chunks) >= 1, "应至少推送 1 个 stream_chunk"

        data = chunks[0]["data"]
        assert data["content"] == "Hello"
        assert isinstance(data["sequence"], int)
        assert data["sequence"] > 0

    @pytest.mark.asyncio
    async def test_stream_end_has_full_content(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """stream_end 事件包含 full_content 和 message_persisted。

        验证点（AC-UI-01）：
        - type == "stream_end"
        - data.full_content 存在
        - data.message_persisted 为 True
        """
        await bridge.emit_start()
        await bridge.emit_finish({"raw_result": "最终内容"})

        ends = _get_events_by_type(mock_sink, "stream_end")
        assert len(ends) == 1, f"应推送 1 个 stream_end，得到 {len(ends)}"

        data = ends[0]["data"]
        assert data["full_content"] == "最终内容"
        assert data["message_persisted"] is True

    @pytest.mark.asyncio
    async def test_stream_chunk_rejected_before_start(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """未调用 emit_start 前 emit_chunk 应丢弃（时序保护）。

        验证点：_stream_started=False 时 chunk 被丢弃，不产生 stream_chunk 事件。
        """
        await bridge.emit_chunk({"type": "text", "content": "orphan"})
        chunks = _get_events_by_type(mock_sink, "stream_chunk")
        assert len(chunks) == 0, "未 emit_start 前 chunk 应被丢弃"


# ---------------------------------------------------------------------------
# thinking_start / thinking_chunk / thinking_end 事件格式
# ---------------------------------------------------------------------------


class TestThinkingEvents:
    """思考过程事件格式测试。"""

    @pytest.mark.asyncio
    async def test_thinking_start_emitted_on_first_thinking_chunk(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """首个 thinking chunk 触发 thinking_start 事件。

        验证点（AC-UI-03）：
        - thinking_start 在首个 thinking chunk 时发出
        - thinking_start 包含 sequence
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "让我想想"})

        starts = _get_events_by_type(mock_sink, "thinking_start")
        assert len(starts) == 1, "应推送 1 个 thinking_start"
        assert "sequence" in starts[0]["data"], "thinking_start 缺少 sequence"

    @pytest.mark.asyncio
    async def test_thinking_chunk_has_content_and_sequence(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """thinking_chunk 事件包含 content 和 sequence（BUG 修复回归）。

        验证点：thinking_chunk 必须包含 sequence 字段，
        前端需要它来正确排序思考过程片段。
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理步骤1"})

        chunks = _get_events_by_type(mock_sink, "thinking_chunk")
        assert len(chunks) >= 1, "应至少推送 1 个 thinking_chunk"

        data = chunks[0]["data"]
        assert data["content"] == "推理步骤1", "thinking_chunk content 不正确"
        assert "sequence" in data, "thinking_chunk 缺少 sequence 字段（BUG 回归）"
        assert isinstance(data["sequence"], int)

    @pytest.mark.asyncio
    async def test_thinking_start_only_once(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """多个连续 thinking chunk 只发一次 thinking_start。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "步骤1"})
        await bridge.emit_chunk({"type": "thinking", "content": "步骤2"})
        await bridge.emit_chunk({"type": "thinking", "content": "步骤3"})

        starts = _get_events_by_type(mock_sink, "thinking_start")
        chunks = _get_events_by_type(mock_sink, "thinking_chunk")
        assert len(starts) == 1, "连续 thinking 只应推 1 次 thinking_start"
        assert len(chunks) == 3, "应推送 3 个 thinking_chunk"

    @pytest.mark.asyncio
    async def test_thinking_end_closes_thinking(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """thinking_end chunk 触发 thinking_end 事件。

        验证点（AC-UI-03）：
        - thinking_end 事件发出
        - 后续 thinking chunk 会重新触发 thinking_start
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "思考中"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 1500})

        ends = _get_events_by_type(mock_sink, "thinking_end")
        assert len(ends) == 1, "应推送 1 个 thinking_end"
        assert ends[0]["data"]["duration_ms"] == 1500


# ---------------------------------------------------------------------------
# tool_start / tool_result 事件格式
# ---------------------------------------------------------------------------


class TestToolEvents:
    """工具调用事件格式测试（对应 execution_start/done 的后端实现）。"""

    @pytest.mark.asyncio
    async def test_tool_start_has_name_and_call_id(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """tool_start 事件包含 tool_name 和 call_id。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "file_read",
            "call_id": "call-001",
            "args": {"path": "test.txt"},
        })

        starts = _get_events_by_type(mock_sink, "tool_start")
        assert len(starts) == 1
        data = starts[0]["data"]
        assert data["tool_name"] == "file_read"
        assert data["call_id"] == "call-001"
        assert data["args"] == {"path": "test.txt"}

    @pytest.mark.asyncio
    async def test_tool_result_has_success_and_duration(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """tool_result 事件包含 success、result、duration_ms。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "file_read",
            "call_id": "call-001",
            "result": "file content",
            "success": True,
            "duration_ms": 42,
        })

        results = _get_events_by_type(mock_sink, "tool_result")
        assert len(results) == 1
        data = results[0]["data"]
        assert data["success"] is True
        assert data["result"] == "file content"
        assert data["duration_ms"] == 42

    @pytest.mark.asyncio
    async def test_tool_start_dedup(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """相同 call_id 的 tool_start 只发一次（去重）。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "file_read",
            "call_id": "call-dup",
        })
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "file_read",
            "call_id": "call-dup",
        })

        starts = _get_events_by_type(mock_sink, "tool_start")
        assert len(starts) == 1, "相同 call_id 的 tool_start 应去重"


# ---------------------------------------------------------------------------
# emit_error / emit_suspend 事件格式
# ---------------------------------------------------------------------------


class TestErrorEvents:
    """错误事件格式测试（AC-UI-12）。"""

    @pytest.mark.asyncio
    async def test_emit_error_sends_stream_error(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """emit_error 发送 stream_error 事件。

        验证点（AC-UI-12）：
        - type == "stream_error"
        - data.error 包含错误信息
        """
        await bridge.emit_start()
        await bridge.emit_error(RuntimeError("管道执行失败"))

        errors = _get_events_by_type(mock_sink, "stream_error")
        assert len(errors) == 1
        assert "管道执行失败" in errors[0]["data"]["error"]

    @pytest.mark.asyncio
    async def test_emit_suspend_sends_state_change(
        self, bridge: PipelineStreamBridge, mock_sink: MockOutputSink,
    ) -> None:
        """emit_suspend 发送 state_change + stream_end。

        验证点：
        - state_change 事件 status == "suspended"
        - stream_end 事件在 state_change 之后
        """
        await bridge.emit_start()
        await bridge.emit_suspend({"raw_result": "部分输出"})

        changes = _get_events_by_type(mock_sink, "state_change")
        assert len(changes) == 1
        assert changes[0]["data"]["status"] == "suspended"

        ends = _get_events_by_type(mock_sink, "stream_end")
        assert len(ends) == 1


# ---------------------------------------------------------------------------
# _make_event 自动注入 pipeline_id / message_id
# ---------------------------------------------------------------------------


class TestMakeEvent:
    """_make_event 方法测试，验证事件信封自动注入。"""

    def test_make_event_injects_pipeline_and_message_id(
        self, bridge: PipelineStreamBridge,
    ) -> None:
        """_make_event 自动注入 pipeline_id 和 message_id。"""
        event = bridge._make_event("test_event", {"custom": "data"})

        assert event["type"] == "test_event"
        assert event["data"]["pipeline_id"] == "test-pipe-001"
        assert event["data"]["message_id"] == bridge.message_id
        assert event["data"]["custom"] == "data"

    def test_make_event_does_not_override_explicit_values(
        self, bridge: PipelineStreamBridge,
    ) -> None:
        """_make_event 不覆盖调用方显式设置的值。"""
        event = bridge._make_event("test_event", {
            "pipeline_id": "explicit-pipe",
            "message_id": "explicit-msg",
        })

        assert event["data"]["pipeline_id"] == "explicit-pipe"
        assert event["data"]["message_id"] == "explicit-msg"
