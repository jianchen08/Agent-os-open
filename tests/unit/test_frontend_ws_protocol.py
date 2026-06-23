"""WebSocket 消息协议测试。

验证前端 WebSocket 通信协议的完整性和正确性。
对应需求：F-UI-01~05, AC-UI-01, AC-UI-12

覆盖的事件类型（来源：需求文档 §2.1 核心事件类型）：
- stream_start / stream_chunk / stream_end
- thinking_start / thinking_chunk / thinking_end
- execution_start / execution_progress / execution_done
- interaction_request / interaction_cancelled
- pipeline_error

验证点：
1. 每个事件的信封格式：type/data/source_type/source_id/timestamp 完整
2. data 字段包含协议要求的关键子字段
3. message_id / pipeline_id 正确注入
4. 思考过程事件时序：thinking_start → thinking_chunk → thinking_end
5. 流式输出时序：stream_start → stream_chunk → stream_end
"""
from __future__ import annotations

from datetime import datetime
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
        self.__thread_id = thread_id
        self.events: list[dict[str, Any]] = []

    async def send_event(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True

    @property
    def _thread_id(self) -> str:
        return self.__thread_id

    @_thread_id.setter
    def _thread_id(self, value: str) -> None:
        self.__thread_id = value


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

REQUIRED_ENVELOPE_FIELDS = {"type", "data", "source_type", "source_id", "timestamp"}


def _get_events_by_type(sink: MockOutputSink, event_type: str) -> list[dict]:
    """从 mock sink 中提取指定类型的事件。"""
    return [e for e in sink.events if e.get("type") == event_type]


def _assert_envelope_fields(event: dict, msg: str = "") -> None:
    """断言事件信封包含协议要求的必须字段。"""
    for field in REQUIRED_ENVELOPE_FIELDS:
        assert field in event, (
            f"事件缺少信封字段 '{field}': type={event.get('type', '?')} {msg}"
        )
    assert isinstance(event["type"], str)
    assert isinstance(event["data"], dict)
    assert isinstance(event["source_type"], str)
    assert isinstance(event["source_id"], str)
    assert isinstance(event["timestamp"], str)


# ---------------------------------------------------------------------------
# Fixtures
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


# ===========================================================================
# 一、流式输出事件协议测试
# ===========================================================================


class TestStreamEvents:
    """stream_start / stream_chunk / stream_end 事件协议测试。"""

    @pytest.mark.asyncio
    async def test_stream_start_envelope_and_data(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """stream_start 事件包含完整信封和必须的 data 字段。

        验证点（F-UI-01, F-UI-02）：
        - 信封字段：type/data/source_type/source_id/timestamp
        - data 包含 message_id, pipeline_id
        """
        await bridge.emit_start()

        events = _get_events_by_type(bridge.output_sink, "stream_start")
        assert len(events) == 1, "应发出 1 个 stream_start 事件"
        event = events[0]

        _assert_envelope_fields(event)
        assert "message_id" in event["data"], "stream_start.data 必须包含 message_id"
        assert "pipeline_id" in event["data"], "stream_start.data 必须包含 pipeline_id"
        assert event["data"]["pipeline_id"] == "test-pipe-001"

    @pytest.mark.asyncio
    async def test_stream_chunk_envelope_and_content(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """stream_chunk 事件包含 content 字段。

        验证点（F-UI-02）：LLM 流式输出每个 token 生成时推送 stream_chunk。
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "hello"})

        events = _get_events_by_type(bridge.output_sink, "stream_chunk")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert "content" in event["data"], "stream_chunk.data 必须包含 content"
        assert event["data"]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_stream_end_envelope_and_fields(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """stream_end 事件包含 full_content 字段。

        验证点（F-UI-02）：LLM 生成完成时推送 stream_end。
        """
        state = {"raw_result": "完整输出内容"}
        await bridge.emit_start()
        await bridge.emit_finish(state)

        events = _get_events_by_type(bridge.output_sink, "stream_end")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert "full_content" in event["data"], "stream_end.data 必须包含 full_content"
        assert event["data"]["full_content"] == "完整输出内容"

    @pytest.mark.asyncio
    async def test_stream_lifecycle_order(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """流式输出完整时序：stream_start → stream_chunk(×N) → stream_end。

        验证点（需求 §4.1 流式输出流程）：事件按正确顺序推送。
        """
        state = {"raw_result": "ABC"}
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "A"})
        await bridge.emit_chunk({"type": "text", "content": "B"})
        await bridge.emit_chunk({"type": "text", "content": "C"})
        await bridge.emit_finish(state)

        type_sequence = [e["type"] for e in bridge.output_sink.events]
        start_idx = type_sequence.index("stream_start")
        end_idx = type_sequence.index("stream_end")
        chunk_indices = [i for i, t in enumerate(type_sequence) if t == "stream_chunk"]

        assert start_idx < end_idx, "stream_start 必须在 stream_end 之前"
        for ci in chunk_indices:
            assert start_idx < ci < end_idx, "stream_chunk 必须在 start 和 end 之间"


# ===========================================================================
# 二、思考过程事件协议测试
# ===========================================================================


class TestThinkingEvents:
    """thinking_start / thinking_chunk / thinking_end 事件协议测试。"""

    @pytest.mark.asyncio
    async def test_thinking_start_on_first_thinking_chunk(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """首次 thinking chunk 自动触发 thinking_start。

        验证点（F-UI-03）：思考过程展示。
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "分析中..."})

        start_events = _get_events_by_type(bridge.output_sink, "thinking_start")
        assert len(start_events) == 1, "首个 thinking chunk 应触发 thinking_start"
        _assert_envelope_fields(start_events[0])

    @pytest.mark.asyncio
    async def test_thinking_chunk_contains_content(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """thinking_chunk 事件包含 content 和 step_type 字段。"""
        await bridge.emit_start()
        await bridge.emit_chunk(
            {"type": "thinking", "content": "第一步推理", "step_type": "analysis"}
        )

        chunk_events = _get_events_by_type(bridge.output_sink, "thinking_chunk")
        assert len(chunk_events) == 1
        event = chunk_events[0]

        _assert_envelope_fields(event)
        assert "content" in event["data"]
        assert event["data"]["content"] == "第一步推理"
        assert "step_type" in event["data"]

    @pytest.mark.asyncio
    async def test_thinking_end_on_close(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """thinking_end 事件在思考过程结束时推送。

        验证点（F-UI-03）：思考过程正确关闭。
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 500})

        end_events = _get_events_by_type(bridge.output_sink, "thinking_end")
        assert len(end_events) == 1
        _assert_envelope_fields(end_events[0])
        assert "duration_ms" in end_events[0]["data"]

    @pytest.mark.asyncio
    async def test_thinking_lifecycle_order(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """思考过程完整时序：thinking_start → thinking_chunk(×N) → thinking_end。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "思考1"})
        await bridge.emit_chunk({"type": "thinking", "content": "思考2"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 300})

        type_seq = [
            e["type"]
            for e in bridge.output_sink.events
            if e["type"].startswith("thinking")
        ]
        assert type_seq[0] == "thinking_start"
        assert type_seq[-1] == "thinking_end"
        assert all(t == "thinking_chunk" for t in type_seq[1:-1]), (
            "中间事件应为 thinking_chunk"
        )


# ===========================================================================
# 三、执行可视化事件协议测试
# ===========================================================================


class TestExecutionEvents:
    """execution_start(→tool_start) / execution_done(→tool_result) 事件协议测试。

    注意：后端 bridge 实际使用 tool_start / tool_result 作为执行事件名，
    对应前端渲染为 ActivityCard（等待态/完成态）。
    """

    @pytest.mark.asyncio
    async def test_tool_start_envelope(self, bridge: PipelineStreamBridge) -> None:
        """tool_start（执行开始）事件包含 tool_name 和 args。"""
        await bridge.emit_start()
        await bridge.emit_chunk(
            {
                "type": "tool_start",
                "tool_name": "file_read",
                "args": {"path": "test.py"},
                "call_id": "call-001",
            }
        )

        events = _get_events_by_type(bridge.output_sink, "tool_start")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert event["data"]["tool_name"] == "file_read"
        assert event["data"]["call_id"] == "call-001"
        assert "args" in event["data"]

    @pytest.mark.asyncio
    async def test_tool_result_envelope(self, bridge: PipelineStreamBridge) -> None:
        """tool_result（执行完成）事件包含 tool_name, success, result, duration_ms。"""
        await bridge.emit_start()
        await bridge.emit_chunk(
            {
                "type": "tool_start",
                "tool_name": "bash_execute",
                "call_id": "call-002",
            }
        )
        await bridge.emit_chunk(
            {
                "type": "tool_result",
                "tool_name": "bash_execute",
                "success": True,
                "result": {"output": "done"},
                "duration_ms": 150,
                "call_id": "call-002",
            }
        )

        events = _get_events_by_type(bridge.output_sink, "tool_result")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert event["data"]["tool_name"] == "bash_execute"
        assert event["data"]["success"] is True
        assert event["data"]["duration_ms"] == 150

    @pytest.mark.asyncio
    async def test_tool_failed_result_envelope(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """tool_result 失败状态正确传播。"""
        await bridge.emit_start()
        await bridge.emit_chunk(
            {
                "type": "tool_start",
                "tool_name": "file_read",
                "call_id": "call-003",
            }
        )
        await bridge.emit_chunk(
            {
                "type": "tool_result",
                "tool_name": "file_read",
                "success": False,
                "result": None,
                "duration_ms": 50,
                "call_id": "call-003",
            }
        )

        events = _get_events_by_type(bridge.output_sink, "tool_result")
        assert len(events) == 1
        assert events[0]["data"]["success"] is False


# ===========================================================================
# 四、交互事件协议测试
# ===========================================================================


class TestInteractionEvents:
    """interaction_request / interaction_cancelled 事件协议测试。"""

    @pytest.mark.asyncio
    async def test_interaction_request_format(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """管道挂起时推送 state_change(suspended) 事件。"""
        await bridge.emit_start()
        await bridge.emit_suspend({"raw_result": "等待审批"})

        state_events = _get_events_by_type(bridge.output_sink, "state_change")
        assert len(state_events) == 1
        event = state_events[0]

        _assert_envelope_fields(event)
        assert event["data"]["status"] == "suspended"
        assert event["data"]["pipeline_id"] == "test-pipe-001"

    @pytest.mark.asyncio
    async def test_emit_suspend_closes_thinking(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """挂起时自动关闭活跃的 thinking。

        验证点：挂起 → thinking_end → state_change(suspended)
        """
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理"})
        await bridge.emit_suspend({"raw_result": ""})

        thinking_end_events = _get_events_by_type(
            bridge.output_sink, "thinking_end"
        )
        assert len(thinking_end_events) == 1, "挂起时应关闭活跃的 thinking"


# ===========================================================================
# 五、错误事件协议测试
# ===========================================================================


class TestErrorEvents:
    """pipeline_error / stream_error 事件协议测试。"""

    @pytest.mark.asyncio
    async def test_emit_error_envelope(self, bridge: PipelineStreamBridge) -> None:
        """管道致命错误推送 stream_error 事件。"""
        await bridge.emit_start()
        await bridge.emit_error(ValueError("测试错误"))

        events = _get_events_by_type(bridge.output_sink, "stream_error")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert "error" in event["data"]
        assert "测试错误" in event["data"]["error"]

    @pytest.mark.asyncio
    async def test_emit_error_closes_thinking(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """错误时自动关闭活跃的 thinking。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理"})
        await bridge.emit_error(RuntimeError("崩溃"))

        thinking_end_events = _get_events_by_type(
            bridge.output_sink, "thinking_end"
        )
        assert len(thinking_end_events) == 1


# ===========================================================================
# 六、系统通知事件协议测试
# ===========================================================================


class TestNotificationEvents:
    """system_notification 事件协议测试。"""

    @pytest.mark.asyncio
    async def test_emit_notification_envelope(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """系统通知推送包含 content, source, level, sequence。"""
        seq = await bridge.emit_notification(
            "任务完成", source="system", level="info"
        )

        events = _get_events_by_type(bridge.output_sink, "system_notification")
        assert len(events) == 1
        event = events[0]

        _assert_envelope_fields(event)
        assert event["data"]["content"] == "任务完成"
        assert event["data"]["source"] == "system"
        assert event["data"]["level"] == "info"
        assert seq >= 0, "通知的 sequence 应非负"

    @pytest.mark.asyncio
    async def test_emit_notification_empty_skipped(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """空内容通知不推送。"""
        seq = await bridge.emit_notification("", source="system")

        assert seq == -1, "空内容通知应返回 -1"
        events = _get_events_by_type(bridge.output_sink, "system_notification")
        assert len(events) == 0


# ===========================================================================
# 七、事件信封字段一致性测试
# ===========================================================================


class TestEnvelopeConsistency:
    """所有事件信封字段一致性测试。"""

    @pytest.mark.asyncio
    async def test_all_events_have_envelope_fields(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """所有推送的事件都包含 type/data/source_type/source_id/timestamp。

        验证点（需求 §2.1 消息信封格式）：协议强制要求。
        """
        state = {"raw_result": "内容", "raw_thinking": "思考"}
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理"})
        await bridge.emit_chunk({"type": "thinking_end"})
        await bridge.emit_chunk({"type": "text", "content": "文字"})
        await bridge.emit_finish(state)

        for event in bridge.output_sink.events:
            _assert_envelope_fields(event)

    @pytest.mark.asyncio
    async def test_source_type_is_valid(self, bridge: PipelineStreamBridge) -> None:
        """所有事件的 source_type 字段是有效字符串。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "hi"})
        await bridge.emit_finish({"raw_result": "hi"})

        valid_sources = {"system", "agent", "user", "tool"}
        for event in bridge.output_sink.events:
            assert event["source_type"] in valid_sources, (
                f"source_type '{event['source_type']}' 不在有效值集合 {valid_sources} 中"
            )

    @pytest.mark.asyncio
    async def test_timestamp_is_iso_format(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """所有事件的 timestamp 字段是 ISO 8601 格式。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "hi"})
        await bridge.emit_finish({"raw_result": "hi"})

        for event in bridge.output_sink.events:
            ts = event["timestamp"]
            assert "T" in ts, f"timestamp 应为 ISO 8601 格式（包含 T）: {ts}"
            datetime.fromisoformat(ts)
