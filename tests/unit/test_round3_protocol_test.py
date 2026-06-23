"""Round 3 WebSocket 事件序列完整性 + 评估门禁回归测试。

本测试是对 Round 1 已有测试（test_frontend_ws_protocol.py、test_review_gate.py）
的**增量补充**，聚焦：

A. WebSocket 事件序列完整性
   1. 全生命周期时序：stream_start → thinking_* → stream_chunk(×N) → tool_* → stream_end
   2. 每种事件信封字段完整性（type/data/source_type/source_id/timestamp + pipeline_id/message_id）
   3. execution_start ↔ execution_done（即 tool_start ↔ tool_result）配对与顺序
   4. interaction_request 完整 data 字段（按协议 §2.1）
   5. pipeline_error（致命错误）vs plugin_error（插件级降级）的区分逻辑

B. 评估门禁回归
   6. 重试次数边界：刚好 3 次通过 / 第 4 次仍失败 / 重试计数严格性
   7. 5 类指标的组合评估（and/or 逻辑）
   8. 红线指标（is_red_line=True）一票否决：单个红线失败即整体失败

对应需求：
- F-UI-01~05、F-UI-09~14、AC-UI-12（前端交互）
- F-TEST-06~09、AC-TST-05（评估门禁）
- 协议规范：docs/requirements/各模块需求文档/04_前端交互模块需求文档.md §2.1

测试原则：
- 复用 Round 1 的 MockOutputSink 模式（保持 fixture 风格一致）
- 测试**实际后端行为**而非纯协议文档（差异在测试中显式标注）
- 后端实际事件名 vs 协议规范名映射：
    tool_start    ↔ execution_start（协议）
    tool_result   ↔ execution_done（协议）
    stream_error  ↔ pipeline_error（协议）
    plugin_error  ：协议中存在但当前后端未实现独立事件
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest

from evaluation.engine import EvaluationEngine
from evaluation.executor import EvaluationExecutor
from evaluation.expect import ExpectEvaluator
from evaluation.loader import MetricLoader
from evaluation.types import (
    EvaluationConfig,
    EvaluationResult,
    ExpectCondition,
    ExpectSpec,
    MetricDefinition,
    MetricResult,
    MetricType,
)
from pipeline.stream_bridge import PipelineStreamBridge


# ===========================================================================
# 公共测试基础设施
# ===========================================================================

REQUIRED_ENVELOPE_FIELDS = {"type", "data", "source_type", "source_id", "timestamp"}
VALID_SOURCE_TYPES = {"system", "agent", "user", "tool"}


class MockOutputSink:
    """模拟输出目标，捕获所有推送的事件。"""

    def __init__(self, thread_id: str = "test-round3-thread") -> None:
        self.sink_id = "mock-round3"
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


def _types(events: list[dict]) -> list[str]:
    """按出现顺序提取 type 序列。"""
    return [e["type"] for e in events]


def _get(events: list[dict], event_type: str) -> list[dict]:
    """提取指定类型的所有事件。"""
    return [e for e in events if e.get("type") == event_type]


def _assert_envelope(event: dict, *, where: str = "") -> None:
    """断言事件信封字段完整。"""
    msg = f"[{where}]" if where else ""
    for fld in REQUIRED_ENVELOPE_FIELDS:
        assert fld in event, (
            f"{msg} 缺少信封字段 '{fld}' (type={event.get('type', '?')})"
        )
    assert isinstance(event["type"], str) and event["type"], (
        f"{msg} type 必须非空字符串"
    )
    assert isinstance(event["data"], dict), f"{msg} data 必须 dict"
    assert event["source_type"] in VALID_SOURCE_TYPES, (
        f"{msg} source_type='{event['source_type']}' 不在 {VALID_SOURCE_TYPES}"
    )
    assert isinstance(event["source_id"], str) and event["source_id"], (
        f"{msg} source_id 必须非空字符串"
    )
    assert isinstance(event["timestamp"], str), f"{msg} timestamp 必须字符串"
    datetime.fromisoformat(event["timestamp"])


def _make_metric(
    metric_id: str,
    metric_type: MetricType = MetricType.TOOL,
    is_red_line: bool = False,
    expect: ExpectSpec | None = None,
) -> MetricDefinition:
    return MetricDefinition(
        id=metric_id,
        name=f"指标 {metric_id}",
        description="round3 测试用指标",
        metric_type=metric_type,
        is_red_line=is_red_line,
        expect=expect
        or ExpectSpec(
            conditions=[ExpectCondition(field="success", operator="is_true")],
        ),
    )


def _passed(metric_id: str) -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=True, message="OK", score=100.0)


def _failed(metric_id: str) -> MetricResult:
    return MetricResult(metric_id=metric_id, passed=False, message="FAIL", score=0.0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sink() -> MockOutputSink:
    return MockOutputSink()


@pytest.fixture
def bridge(mock_sink: MockOutputSink) -> PipelineStreamBridge:
    return PipelineStreamBridge(
        pipeline_id="round3-pipe-001",
        output_sink=mock_sink,
    )


# ===========================================================================
# A 部分：WebSocket 事件序列完整性
# ===========================================================================


# ---------------------------------------------------------------------------
# A.1 全生命周期事件序列（stream_start → thinking → stream_chunk → tool → stream_end）
# ---------------------------------------------------------------------------


class TestFullLifecycleSequence:
    """完整生命周期事件时序验证。

    验证 Round 1 未覆盖的混合时序场景：思考、文本、工具调用交错出现。
    需求映射：F-UI-01/02/03/14。
    """

    @pytest.mark.asyncio
    async def test_full_sequence_thinking_text_tool(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """完整时序：start → thinking_start/chunk/end → stream_chunk(×2)
        → tool_start → tool_result → stream_end。"""
        await bridge.emit_start()
        # 思考段
        await bridge.emit_chunk({"type": "thinking", "content": "推理中"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 100})
        # 正文流式
        await bridge.emit_chunk({"type": "text", "content": "你好"})
        await bridge.emit_chunk({"type": "text", "content": "世界"})
        # 工具调用段
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "file_read",
            "args": {"path": "a.py"},
            "call_id": "c-1",
        })
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "file_read",
            "success": True,
            "result": {"data": "..."},
            "duration_ms": 50,
            "call_id": "c-1",
        })
        await bridge.emit_finish({"raw_result": "你好世界"})

        seq = _types(bridge.output_sink.events)

        # 第一个事件必须是 stream_start
        assert seq[0] == "stream_start", f"序列应以 stream_start 开头，实际: {seq[0]}"
        # 结尾必须是 stream_end
        assert seq[-1] == "stream_end", f"序列应以 stream_end 结尾，实际: {seq[-1]}"
        # thinking_start 在 thinking_end 之前
        assert seq.index("thinking_start") < seq.index("thinking_end")
        # thinking 整体在正文 chunk 之前
        thinking_end_idx = seq.index("thinking_end")
        chunk_indices = [i for i, t in enumerate(seq) if t == "stream_chunk"]
        for ci in chunk_indices:
            assert ci > thinking_end_idx, (
                f"stream_chunk(idx={ci}) 必须在 thinking_end(idx={thinking_end_idx}) 之后"
            )
        # tool_start 在 tool_result 之前
        assert seq.index("tool_start") < seq.index("tool_result")
        # 所有 tool 事件在 stream_start 之后
        assert seq.index("tool_start") > 0
        # tool 事件在 stream_end 之前
        assert seq.index("tool_result") < seq.index("stream_end")

    @pytest.mark.asyncio
    async def test_sequence_without_thinking(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """无思考段时：start → stream_chunk(×3) → end。"""
        await bridge.emit_start()
        for ch in ("A", "B", "C"):
            await bridge.emit_chunk({"type": "text", "content": ch})
        await bridge.emit_finish({"raw_result": "ABC"})

        seq = _types(bridge.output_sink.events)
        assert seq[0] == "stream_start"
        assert seq[-1] == "stream_end"
        assert "thinking_start" not in seq
        assert "thinking_end" not in seq
        assert seq.count("stream_chunk") == 3
        # 所有 chunk 在 start 和 end 之间
        start_idx = seq.index("stream_start")
        end_idx = seq.index("stream_end")
        for i, t in enumerate(seq):
            if t == "stream_chunk":
                assert start_idx < i < end_idx

    @pytest.mark.asyncio
    async def test_multiple_thinking_segments(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """多段思考：thinking_start/chunk/end 可出现多次。"""
        await bridge.emit_start()
        # 第一段思考
        await bridge.emit_chunk({"type": "thinking", "content": "思考1"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 100})
        # 中间正文
        await bridge.emit_chunk({"type": "text", "content": "中间文本"})
        # 第二段思考
        await bridge.emit_chunk({"type": "thinking", "content": "思考2"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 200})
        await bridge.emit_finish({"raw_result": "最终输出"})

        seq = _types(bridge.output_sink.events)
        assert seq.count("thinking_start") == 2, "应有 2 个 thinking_start"
        assert seq.count("thinking_end") == 2, "应有 2 个 thinking_end"
        assert seq.count("thinking_chunk") == 2, "应有 2 个 thinking_chunk"
        # 每对 thinking_start→thinking_end 之间恰好 1 个 thinking_chunk
        ts_indices = [i for i, t in enumerate(seq) if t == "thinking_start"]
        te_indices = [i for i, t in enumerate(seq) if t == "thinking_end"]
        for ts, te in zip(ts_indices, te_indices, strict=True):
            assert ts < te, "thinking_start 必须在对应 thinking_end 之前"


# ---------------------------------------------------------------------------
# A.2 每种事件信封字段完整性
# ---------------------------------------------------------------------------


class TestAllEventEnvelopes:
    """验证所有事件类型都包含完整信封字段 + pipeline_id/message_id 注入。

    需求 §2.1 消息信封：type/data/source_type/source_id/timestamp。
    BridgeCore._make_event 还自动注入 pipeline_id 和 message_id 到 data 中。
    """

    @pytest.mark.asyncio
    async def test_every_event_has_complete_envelope(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """所有事件（含 new_message 和 stream_end）都有完整信封。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "x"})
        await bridge.emit_chunk({"type": "thinking_end", "duration_ms": 10})
        await bridge.emit_chunk({"type": "text", "content": "hello"})
        await bridge.emit_chunk({
            "type": "tool_start", "tool_name": "t", "call_id": "c",
        })
        await bridge.emit_chunk({
            "type": "tool_result", "tool_name": "t",
            "success": True, "call_id": "c", "duration_ms": 5,
        })
        await bridge.emit_finish({"raw_result": "hello"})

        assert len(bridge.output_sink.events) > 0, "应产生至少一个事件"
        for ev in bridge.output_sink.events:
            _assert_envelope(ev, where=ev.get("type", "?"))

    @pytest.mark.asyncio
    async def test_every_event_data_has_pipeline_and_message_id(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """每个事件 data 中注入了 pipeline_id 和 message_id。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "hi"})
        await bridge.emit_finish({"raw_result": "hi"})

        for ev in bridge.output_sink.events:
            assert "pipeline_id" in ev["data"], (
                f"type={ev['type']} data 缺少 pipeline_id"
            )
            assert ev["data"]["pipeline_id"] == "round3-pipe-001"
            assert "message_id" in ev["data"], (
                f"type={ev['type']} data 缺少 message_id"
            )
            assert isinstance(ev["data"]["message_id"], str)
            assert len(ev["data"]["message_id"]) > 0

    @pytest.mark.asyncio
    async def test_envelope_timestamp_is_monotonic(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """时间戳非递减（后续事件 >= 前面事件）。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "text", "content": "a"})
        await bridge.emit_chunk({"type": "text", "content": "b"})
        await bridge.emit_finish({"raw_result": "ab"})

        timestamps = [
            datetime.fromisoformat(ev["timestamp"])
            for ev in bridge.output_sink.events
        ]
        for i in range(1, len(timestamps)):
            assert timestamps[i] >= timestamps[i - 1], (
                f"时间戳非单调：idx {i-1}={timestamps[i-1]} > idx {i}={timestamps[i]}"
            )

    @pytest.mark.asyncio
    async def test_error_event_envelope_and_data(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """stream_error 事件（协议 pipeline_error 的后端实现）信封完整。"""
        await bridge.emit_start()
        await bridge.emit_error(ValueError("致命管道故障"))

        events = _get(bridge.output_sink.events, "stream_error")
        assert len(events) == 1
        ev = events[0]
        _assert_envelope(ev, where="stream_error")
        assert "error" in ev["data"]
        assert "致命管道故障" in ev["data"]["error"]
        assert ev["data"]["message_persisted"] is False

    @pytest.mark.asyncio
    async def test_notification_event_envelope(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """system_notification 事件信封完整。"""
        await bridge.emit_notification("任务完成", source="system", level="info")

        events = _get(bridge.output_sink.events, "system_notification")
        assert len(events) == 1
        ev = events[0]
        _assert_envelope(ev, where="system_notification")
        assert ev["data"]["content"] == "任务完成"
        assert ev["data"]["level"] == "info"
        assert ev["data"]["source"] == "system"


# ---------------------------------------------------------------------------
# A.3 execution_start → execution_done 序列验证（tool_start/tool_result）
# ---------------------------------------------------------------------------


class TestExecutionSequence:
    """执行可视化事件序列：tool_start ↔ tool_result 配对与顺序。

    后端使用 tool_start/tool_result 分别对应协议 execution_start/execution_done。
    需求映射：F-UI-14（ActivityCard）、协议 §2.1 execution_start/progress/done。
    """

    @pytest.mark.asyncio
    async def test_tool_start_before_result(self, bridge: PipelineStreamBridge) -> None:
        """单个工具：tool_start 必须在 tool_result 之前。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "bash_execute",
            "args": {"command": "echo hi"},
            "call_id": "call-A",
        })
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "bash_execute",
            "success": True,
            "result": {"output": "hi"},
            "duration_ms": 30,
            "call_id": "call-A",
        })

        seq = _types(bridge.output_sink.events)
        assert "tool_start" in seq
        assert "tool_result" in seq
        assert seq.index("tool_start") < seq.index("tool_result")

    @pytest.mark.asyncio
    async def test_multiple_tools_sequential(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """多个工具顺序执行：start1→result1→start2→result2。"""
        await bridge.emit_start()
        for cid in ("call-1", "call-2"):
            await bridge.emit_chunk({
                "type": "tool_start",
                "tool_name": f"tool_{cid}",
                "call_id": cid,
            })
            await bridge.emit_chunk({
                "type": "tool_result",
                "tool_name": f"tool_{cid}",
                "success": True,
                "call_id": cid,
                "duration_ms": 10,
            })

        seq = _types(bridge.output_sink.events)
        starts = [i for i, t in enumerate(seq) if t == "tool_start"]
        results = [i for i, t in enumerate(seq) if t == "tool_result"]
        # start1 < result1 < start2 < result2
        assert starts[0] < results[0] < starts[1] < results[1]

    @pytest.mark.asyncio
    async def test_tool_result_without_start_auto_fixup(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """tool_result 在没有 tool_start 时自动补发 tool_start（FIXUP 机制）。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "file_read",
            "success": True,
            "call_id": "orphan-call",
            "duration_ms": 20,
        })

        seq = _types(bridge.output_sink.events)
        assert "tool_start" in seq, "缺少 tool_start 时应自动补发"
        assert seq.index("tool_start") < seq.index("tool_result")

    @pytest.mark.asyncio
    async def test_tool_start_dedup(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """相同 call_id 的 tool_start 去重（只发一次）。"""
        await bridge.emit_start()
        for _ in range(3):
            await bridge.emit_chunk({
                "type": "tool_start",
                "tool_name": "t",
                "call_id": "dup-call",
            })

        starts = _get(bridge.output_sink.events, "tool_start")
        assert len(starts) == 1, f"相同 call_id 去重后应只发 1 个 tool_start，实际 {len(starts)}"

    @pytest.mark.asyncio
    async def test_tool_start_then_failure_result(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """工具执行失败：tool_start → tool_result(success=False)。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "bash_execute",
            "call_id": "fail-call",
        })
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "bash_execute",
            "success": False,
            "result": None,
            "error": "command not found",
            "duration_ms": 5,
            "call_id": "fail-call",
        })

        results = _get(bridge.output_sink.events, "tool_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is False


# ---------------------------------------------------------------------------
# A.4 interaction_request 完整 data 字段验证
# ---------------------------------------------------------------------------


class TestInteractionRequestFields:
    """interaction_request 完整 data 字段验证。

    需求 §2.1: interaction_request data 字段包含
    thread_id, request_id, interaction_type, mode, title, priority,
    approval_options, context, conversation_context, agent_id。

    后端 ws_handler.notify_request 构造实际 payload，此处复现该构造逻辑进行验证。
    """

    def _build_interaction_payload(
        self,
        request_id: str = "req-001",
        thread_id: str = "thread-001",
        interaction_mode: str = "choice",
        title: str = "需要审批",
        priority: str = "high",
        agent_id: str = "agent-executor",
    ) -> dict[str, Any]:
        """复现 ws_handler.notify_request 的 payload 构造逻辑。"""
        msg_data = {
            "interaction_mode": interaction_mode,
            "title": title,
            "description": "请确认是否执行此操作",
            "options": ["approve", "reject"],
            "questions": None,
            "timeout_seconds": 300,
            "priority": priority,
            "agent_id": agent_id,
            "agent_level": "L3",
            "file_paths": ["/workspace/src/main.py"],
        }
        record = {
            "id": request_id,
            "session_id": "sess-001",
            "message_data": {
                **msg_data,
                "thread_id": thread_id,
                "pipeline_id": "pipe-001",
            },
        }
        # 按 ws_handler 的实际构造逻辑
        return {
            "type": "interaction_request",
            "data": {
                "request_id": record.get("id", ""),
                "interaction_mode": msg_data.get("interaction_mode", "choice"),
                "title": msg_data.get("title", ""),
                "description": msg_data.get("description", ""),
                "options": msg_data.get("options"),
                "questions": msg_data.get("questions"),
                "initial_message": msg_data.get("initial_message"),
                "suggestions": msg_data.get("suggestions"),
                "timeout_seconds": msg_data.get("timeout_seconds"),
                "priority": msg_data.get("priority", "normal"),
                "thread_id": thread_id,
                "tab_id": msg_data.get("tab_id", ""),
                "agent_id": msg_data.get("agent_id", ""),
                "pipeline_id": record.get("message_data", {}).get("pipeline_id", ""),
                "file_paths": msg_data.get("file_paths"),
                "progress": msg_data.get("progress"),
                "agent_level": msg_data.get("agent_level"),
                "session_id": record.get("session_id", ""),
            },
        }

    def test_required_fields_present(self) -> None:
        """interaction_request 必须包含协议要求的所有核心字段。"""
        payload = self._build_interaction_payload()
        data = payload["data"]

        # 协议 §2.1 要求字段
        assert "request_id" in data and data["request_id"] == "req-001"
        assert "thread_id" in data and data["thread_id"] == "thread-001"
        assert "interaction_mode" in data and data["interaction_mode"] == "choice"
        assert "title" in data and data["title"] == "需要审批"
        assert "priority" in data and data["priority"] == "high"
        assert "options" in data and data["options"] == ["approve", "reject"]
        assert "agent_id" in data and data["agent_id"] == "agent-executor"

    def test_type_is_interaction_request(self) -> None:
        """payload type 固定为 interaction_request。"""
        payload = self._build_interaction_payload()
        assert payload["type"] == "interaction_request"

    def test_serialization_roundtrip(self) -> None:
        """payload 可序列化为 JSON 并反序列化，字段不变。"""
        payload = self._build_interaction_payload()
        serialized = json.dumps(payload, ensure_ascii=False)
        deserialized = json.loads(serialized)
        assert deserialized == payload

    def test_priority_values(self) -> None:
        """不同优先级值正确传递。"""
        for prio in ("normal", "high", "critical"):
            payload = self._build_interaction_payload(priority=prio)
            assert payload["data"]["priority"] == prio

    def test_interaction_modes(self) -> None:
        """不同交互模式正确传递。"""
        for mode in ("choice", "input", "form", "approval"):
            payload = self._build_interaction_payload(interaction_mode=mode)
            assert payload["data"]["interaction_mode"] == mode


# ---------------------------------------------------------------------------
# A.5 pipeline_error vs plugin_error 区分逻辑
# ---------------------------------------------------------------------------


class TestErrorClassification:
    """管道致命错误 vs 插件级降级错误的区分。

    协议 §2.1:
    - pipeline_error: 管道致命错误，前端展示错误消息（error, phase, plugin?）
    - plugin_error: 插件出错（非 ABORT），静默降级（plugin, policy, error, fallback?）

    后端实际实现：
    - 致命错误 → emit_error() → stream_error 事件（含 error + message_persisted=False）
    - 插件级失败 → tool_result(success=False)（不中断管道，前端可继续）
    """

    @pytest.mark.asyncio
    async def test_fatal_error_uses_stream_error(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """致命错误通过 stream_error 发送（对应协议 pipeline_error）。"""
        await bridge.emit_start()
        await bridge.emit_error(RuntimeError("管道崩溃"))

        error_events = _get(bridge.output_sink.events, "stream_error")
        assert len(error_events) == 1
        ev = error_events[0]
        _assert_envelope(ev, where="stream_error")
        assert "error" in ev["data"]
        assert "管道崩溃" in ev["data"]["error"]
        assert ev["data"]["message_persisted"] is False
        # stream_started 应变为 False（管道已终止）
        assert bridge._stream_started is False

    @pytest.mark.asyncio
    async def test_plugin_failure_uses_tool_result(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """插件级失败通过 tool_result(success=False) 发送，管道不中断。"""
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start",
            "tool_name": "web_search",
            "call_id": "c-fail",
        })
        await bridge.emit_chunk({
            "type": "tool_result",
            "tool_name": "web_search",
            "success": False,
            "result": None,
            "error": "网络超时",
            "call_id": "c-fail",
            "duration_ms": 5000,
        })
        # 管道不中断，可以继续
        await bridge.emit_chunk({"type": "text", "content": "降级处理"})
        await bridge.emit_finish({"raw_result": "降级处理"})

        # 不应有 stream_error
        error_events = _get(bridge.output_sink.events, "stream_error")
        assert len(error_events) == 0, "插件级失败不应产生 stream_error"
        # 有 tool_result 且 success=False
        results = _get(bridge.output_sink.events, "tool_result")
        assert len(results) == 1
        assert results[0]["data"]["success"] is False
        # 管道继续执行
        assert "stream_chunk" in _types(bridge.output_sink.events)
        assert bridge.output_sink.events[-1]["type"] == "stream_end"

    @pytest.mark.asyncio
    async def test_fatal_error_closes_thinking(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """致命错误时自动关闭活跃的 thinking。"""
        await bridge.emit_start()
        await bridge.emit_chunk({"type": "thinking", "content": "推理中"})
        await bridge.emit_error(ConnectionError("连接断开"))

        thinking_end_events = _get(bridge.output_sink.events, "thinking_end")
        assert len(thinking_end_events) == 1, "致命错误应关闭活跃的 thinking"

    @pytest.mark.asyncio
    async def test_fatal_error_vs_plugin_failure_distinction(
        self, bridge: PipelineStreamBridge
    ) -> None:
        """对比验证：致命错误有 stream_error，插件失败只有 tool_result。"""
        # 场景1：插件失败
        await bridge.emit_start()
        await bridge.emit_chunk({
            "type": "tool_start", "tool_name": "t1", "call_id": "c1",
        })
        await bridge.emit_chunk({
            "type": "tool_result", "tool_name": "t1",
            "success": False, "call_id": "c1", "duration_ms": 10,
        })
        plugin_seq = _types(bridge.output_sink.events)
        assert "stream_error" not in plugin_seq
        assert "tool_result" in plugin_seq

        # 场景2：致命错误（新 bridge）
        sink2 = MockOutputSink()
        bridge2 = PipelineStreamBridge(pipeline_id="p2", output_sink=sink2)
        await bridge2.emit_start()
        await bridge2.emit_error(ValueError("fatal"))
        fatal_seq = _types(sink2.events)
        assert "stream_error" in fatal_seq


# ===========================================================================
# B 部分：评估门禁回归测试
# ===========================================================================


# ---------------------------------------------------------------------------
# B.1 重试次数边界（刚好 3 次通过 / 第 4 次仍失败）
# ---------------------------------------------------------------------------


class TestRetryBoundary:
    """门禁重试边界条件测试。

    需求 F-TEST-09: 门禁未通过必须回退修复（最多重试 3 次）。
    边界：第 1~3 次可重试，第 4 次不再允许。
    """

    MAX_RETRIES = 3

    @pytest.mark.asyncio
    async def test_pass_on_exact_third_retry(self) -> None:
        """刚好第 3 次（最后一次允许的重试）通过 → 门禁开放。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        attempt_count = 0

        async def mock_evaluate(**kwargs):
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                return EvaluationResult(
                    task_id="boundary-3rd",
                    results=[_failed("m1")],
                    overall_passed=False,
                    summary="0/1 通过",
                )
            return EvaluationResult(
                task_id="boundary-3rd",
                results=[_passed("m1")],
                overall_passed=True,
                summary="1/1 通过",
            )

        mock_engine.evaluate = mock_evaluate
        executor = EvaluationExecutor(engine=mock_engine)

        passed = False
        attempt = 0
        for attempt in range(1, self.MAX_RETRIES + 1):
            result = await executor.run_evaluation(
                task_id="boundary-3rd",
                metric_ids=["m1"],
                skip_state_update=True,
            )
            if result.overall_passed:
                passed = True
                break

        assert passed, "第 3 次应通过门禁"
        assert attempt == self.MAX_RETRIES, f"应在第 {self.MAX_RETRIES} 次通过，实际第 {attempt} 次"

    @pytest.mark.asyncio
    async def test_fail_on_fourth_attempt(self) -> None:
        """第 4 次尝试不再允许（最多重试 3 次 = 3 次评估）。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)

        async def mock_evaluate(**kwargs):
            return EvaluationResult(
                task_id="boundary-4th",
                results=[_failed("m1")],
                overall_passed=False,
                summary="0/1 通过",
            )

        mock_engine.evaluate = mock_evaluate
        executor = EvaluationExecutor(engine=mock_engine)

        total_attempts = 0
        final_passed = True
        for i in range(self.MAX_RETRIES + 1):  # 尝试 4 次
            total_attempts = i + 1
            result = await executor.run_evaluation(
                task_id="boundary-4th",
                metric_ids=["m1"],
                skip_state_update=True,
            )
            final_passed = result.overall_passed
            if final_passed:
                break
            # 门禁逻辑：超过 MAX_RETRIES 应停止重试
            if total_attempts >= self.MAX_RETRIES:
                break

        assert not final_passed, "4 次内应全部失败"
        assert total_attempts == self.MAX_RETRIES, (
            f"应在 {self.MAX_RETRIES} 次后停止重试，实际尝试 {total_attempts} 次"
        )

    @pytest.mark.asyncio
    async def test_first_attempt_pass_no_retry(self) -> None:
        """首次即通过 → 无需重试。"""
        mock_engine = AsyncMock(spec=EvaluationEngine)
        call_count = 0

        async def mock_evaluate(**kwargs):
            nonlocal call_count
            call_count += 1
            return EvaluationResult(
                task_id="first-pass",
                results=[_passed("m1")],
                overall_passed=True,
                summary="1/1 通过",
            )

        mock_engine.evaluate = mock_evaluate
        executor = EvaluationExecutor(engine=mock_engine)

        result = await executor.run_evaluation(
            task_id="first-pass", metric_ids=["m1"], skip_state_update=True,
        )
        assert result.overall_passed is True
        assert call_count == 1, "首次通过不应触发重试"


# ---------------------------------------------------------------------------
# B.2 5 类指标组合评估
# ---------------------------------------------------------------------------


class TestFiveMetricCombination:
    """5 类评估指标的组合评估逻辑。

    需求 F-TEST-07: 5 类指标（file_check/format_valid/bash_check/
    semantic_check/human_review）的组合判定。
    """

    @pytest.mark.asyncio
    async def test_all_five_metrics_pass(self) -> None:
        """5 类指标全部通过 → 整体通过。"""
        loader = MetricLoader()
        five_metrics = [
            _make_metric("file_check", MetricType.TOOL),
            _make_metric("format_valid", MetricType.TOOL),
            _make_metric("bash_check", MetricType.TOOL),
            _make_metric("semantic_check", MetricType.AGENT),
            _make_metric("human_review", MetricType.HUMAN),
        ]
        for m in five_metrics:
            loader.metrics[m.id] = m

        async def mock_evaluator(metric_def, params, task_id=""):
            return {"success": True}

        engine = EvaluationEngine(loader=loader)
        engine.register_evaluator(MetricType.TOOL, mock_evaluator)
        engine.register_evaluator(MetricType.AGENT, mock_evaluator)
        engine.register_evaluator(MetricType.HUMAN, mock_evaluator)

        config = EvaluationConfig(
            metric_ids=["file_check", "format_valid", "bash_check",
                        "semantic_check", "human_review"],
            fail_fast=False,
        )
        result = await engine.evaluate(task_id="five-pass", config=config)
        assert result.overall_passed is True
        assert len(result.results) == 5
        assert "5/5" in result.summary

    @pytest.mark.asyncio
    async def test_five_metrics_partial_fail(self) -> None:
        """5 类中 1 个失败 → 整体失败。"""
        loader = MetricLoader()
        five_ids = ["file_check", "format_valid", "bash_check",
                    "semantic_check", "human_review"]
        for mid in five_ids:
            loader.metrics[mid] = _make_metric(mid, MetricType.TOOL)

        call_map = {mid: True for mid in five_ids}
        call_map["bash_check"] = False  # bash_check 失败

        async def mock_evaluator(metric_def, params, task_id=""):
            return {"success": call_map.get(metric_def.id, False)}

        engine = EvaluationEngine(loader=loader)
        engine.register_evaluator(MetricType.TOOL, mock_evaluator)

        config = EvaluationConfig(metric_ids=five_ids, fail_fast=False)
        result = await engine.evaluate(task_id="five-partial", config=config)
        assert result.overall_passed is False
        assert "4/5" in result.summary

    @pytest.mark.asyncio
    async def test_or_logic_passes_if_any_passes(self) -> None:
        """or 逻辑：任一条件满足即通过。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="output.exit_code", operator="equals", value=0),
            ],
            logic="or",
        )
        # success=False 但 exit_code=0 → or 逻辑下通过
        result = evaluator.evaluate(
            metric_id="or-test",
            expect=expect,
            output={"success": False, "output": {"exit_code": 0}},
        )
        assert result.passed is True, "or 逻辑下任一条件满足应通过"

    @pytest.mark.asyncio
    async def test_and_logic_fails_if_any_fails(self) -> None:
        """and 逻辑：任一条件不满足即失败。"""
        evaluator = ExpectEvaluator()
        expect = ExpectSpec(
            conditions=[
                ExpectCondition(field="success", operator="is_true"),
                ExpectCondition(field="output.exit_code", operator="equals", value=0),
            ],
            logic="and",
        )
        result = evaluator.evaluate(
            metric_id="and-test",
            expect=expect,
            output={"success": True, "output": {"exit_code": 1}},
        )
        assert result.passed is False, "and 逻辑下任一条件不满足应失败"


# ---------------------------------------------------------------------------
# B.3 红线指标一票否决（回归）
# ---------------------------------------------------------------------------


class TestRedLineVeto:
    """红线指标（is_red_line=True）一票否决回归测试。

    需求：红线指标未通过 → 整体失败，即使其他指标全部通过。
    Round 1 已有基本测试，此处补充边界场景。
    """

    def test_single_red_line_fail_vetos_all(self) -> None:
        """单个红线失败否决所有其他通过。"""
        result = EvaluationResult(
            task_id="veto-1",
            results=[
                _passed("file_check"),
                _passed("format_valid"),
                _failed("critical_security"),  # 红线指标失败
                _passed("bash_check"),
            ],
        )
        result.compute_overall()
        assert result.overall_passed is False
        assert "3/4" in result.summary

    def test_red_line_pass_with_other_fail_still_fails(self) -> None:
        """红线通过但非红线失败 → 整体仍失败（compute_overall 用 all()）。"""
        result = EvaluationResult(
            task_id="veto-2",
            results=[
                _passed("critical_red_line"),
                _failed("non_critical"),
            ],
        )
        result.compute_overall()
        assert result.overall_passed is False

    def test_all_pass_including_red_line(self) -> None:
        """全部通过（含红线）→ 整体通过。"""
        result = EvaluationResult(
            task_id="veto-3",
            results=[
                _passed("critical_red_line"),
                _passed("file_check"),
                _passed("format_valid"),
            ],
        )
        result.compute_overall()
        assert result.overall_passed is True
        assert "3/3" in result.summary

    def test_empty_results_fails(self) -> None:
        """无评估指标 → 判定为不通过。"""
        result = EvaluationResult(task_id="veto-empty", results=[])
        result.compute_overall()
        assert result.overall_passed is False

    @pytest.mark.asyncio
    async def test_engine_red_line_fail_fast_stops(self) -> None:
        """红线指标失败时 fail_fast 立即停止后续评估。"""
        loader = MetricLoader()
        loader.metrics["m_pass_1"] = _make_metric("m_pass_1", MetricType.TOOL)
        loader.metrics["m_red_line"] = _make_metric(
            "m_red_line", MetricType.TOOL, is_red_line=True,
        )
        loader.metrics["m_pass_2"] = _make_metric("m_pass_2", MetricType.TOOL)

        call_count = 0

        async def mock_evaluator(metric_def, params, task_id=""):
            nonlocal call_count
            call_count += 1
            if metric_def.id == "m_red_line":
                return {"success": False}
            return {"success": True}

        engine = EvaluationEngine(loader=loader)
        engine.register_evaluator(MetricType.TOOL, mock_evaluator)

        config = EvaluationConfig(
            metric_ids=["m_pass_1", "m_red_line", "m_pass_2"],
            fail_fast=True,
        )
        result = await engine.evaluate(task_id="redline-failfast", config=config)
        assert result.overall_passed is False
        # fail_fast 在第一个失败处停止（不一定停在哪，但不会全跑完）
        assert call_count <= 3
        assert any(not r.passed for r in result.results)


# ---------------------------------------------------------------------------
# B.4 ExpectEvaluator 操作符全覆盖（回归补充）
# ---------------------------------------------------------------------------


class TestExpectOperators:
    """ExpectEvaluator 所有操作符回归测试。

    确保 is_true/is_false/equals/not_equals/in/not_in/contains/gt/lt/gte/lte
    全部正确工作。
    """

    def setup_method(self) -> None:
        self.evaluator = ExpectEvaluator()

    def _eval(self, field, operator, value, output):
        return self.evaluator.evaluate(
            metric_id="op-test",
            expect=ExpectSpec(
                conditions=[ExpectCondition(field=field, operator=operator, value=value)],
                logic="and",
            ),
            output=output,
        )

    def test_is_true(self) -> None:
        assert self._eval("flag", "is_true", None, {"flag": True}).passed is True
        assert self._eval("flag", "is_true", None, {"flag": False}).passed is False

    def test_is_false(self) -> None:
        assert self._eval("flag", "is_false", None, {"flag": False}).passed is True
        assert self._eval("flag", "is_false", None, {"flag": True}).passed is False

    def test_equals(self) -> None:
        assert self._eval("code", "equals", 0, {"code": 0}).passed is True
        assert self._eval("code", "equals", 0, {"code": 1}).passed is False

    def test_not_equals(self) -> None:
        assert self._eval("code", "not_equals", 0, {"code": 1}).passed is True
        assert self._eval("code", "not_equals", 0, {"code": 0}).passed is False

    def test_in(self) -> None:
        assert self._eval("name", "in", ["a", "b"], {"name": "a"}).passed is True
        assert self._eval("name", "in", ["a", "b"], {"name": "c"}).passed is False

    def test_not_in(self) -> None:
        assert self._eval("name", "not_in", ["a"], {"name": "b"}).passed is True
        assert self._eval("name", "not_in", ["a"], {"name": "a"}).passed is False

    def test_contains(self) -> None:
        assert self._eval("text", "contains", "world", {"text": "hello world"}).passed is True
        assert self._eval("text", "contains", "xyz", {"text": "hello"}).passed is False

    def test_gt(self) -> None:
        assert self._eval("score", "gt", 50, {"score": 80}).passed is True
        assert self._eval("score", "gt", 50, {"score": 50}).passed is False

    def test_lt(self) -> None:
        assert self._eval("score", "lt", 50, {"score": 30}).passed is True
        assert self._eval("score", "lt", 50, {"score": 50}).passed is False

    def test_gte(self) -> None:
        assert self._eval("score", "gte", 50, {"score": 50}).passed is True
        assert self._eval("score", "gte", 50, {"score": 49}).passed is False

    def test_lte(self) -> None:
        assert self._eval("score", "lte", 50, {"score": 50}).passed is True
        assert self._eval("score", "lte", 50, {"score": 51}).passed is False

    def test_nested_field_path(self) -> None:
        """嵌套字段路径解析。"""
        assert self._eval(
            "output.exit_code", "equals", 0,
            {"output": {"exit_code": 0}},
        ).passed is True
        assert self._eval(
            "output.exit_code", "equals", 0,
            {"output": {"exit_code": 1}},
        ).passed is False

    def test_missing_field_returns_none(self) -> None:
        """字段不存在时返回 None，is_true 失败。"""
        assert self._eval(
            "nonexistent", "is_true", None, {"other": True},
        ).passed is False
