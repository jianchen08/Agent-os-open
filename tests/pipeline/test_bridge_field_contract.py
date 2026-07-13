"""流式桥接字段契约单元测试。

钉死两个数据源的字段命名差异（来自 e2e_ws_tool_flow 验证逻辑）：
- 实时事件 tool_start / tool_result：snake_case（tool_name / call_id）
  来源 bridge_events._handle_chunk
- parts[] 的 tool_call 子项：camelCase（name / callId）
  来源 bridge_core._build_parts_from_state

二者刻意不同——按数据源划分，不是 bug。任何一边被误改（如 call_id→callId）
都会破坏前端 toolHandler 的双读对齐，本测试用于守住这个契约。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.stream_bridge import PipelineStreamBridge
from pipeline.sink import IOutputSink


class _EventSink(IOutputSink):
    """Mock Sink：记录所有推送事件。"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def send_event(self, event: dict[str, Any]) -> bool:
        self.events.append(event)
        return True

    @property
    def sink_id(self) -> str:
        return "contract-sink"


def _run(coro: Any) -> Any:
    """安全执行 async 函数（兼容已在事件循环内的场景）。"""
    try:
        asyncio.get_running_loop()
        import concurrent.futures  # noqa: PLC0415
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


@pytest.fixture
def sink() -> _EventSink:
    return _EventSink()


@pytest.fixture
def bridge(sink: _EventSink) -> PipelineStreamBridge:
    return PipelineStreamBridge("pipe-contract", sink, message_id="msg-contract")


# ============================================================
# 实时事件契约：tool_start / tool_result = snake_case
# ============================================================


class TestRealtimeEventContract:
    """实时 WS 事件（tool_start/tool_result）字段必须是 snake_case。

    数据源：bridge_events._handle_chunk → _make_event。
    前端 toolHandler 用 `call_id || data?.call_id` 双读对齐此契约。
    """

    def test_tool_start_uses_snake_case(self, bridge: PipelineStreamBridge, sink: _EventSink) -> None:
        """tool_start 事件 data 含 tool_name / call_id（snake_case），不含 camelCase。"""
        chunk = {
            "type": "tool_start",
            "tool_name": "bash_execute",
            "call_id": "call-abc-001",
            "args": {"command": "echo hi"},
        }
        _run(bridge._handle_chunk(chunk))

        starts = [e for e in sink.events if e["type"] == "tool_start"]
        assert len(starts) == 1, f"应发一个 tool_start，得到 {len(starts)}"

        data = starts[0]["data"]
        # snake_case 契约
        assert "tool_name" in data, "tool_start.data 必须含 tool_name（snake_case）"
        assert "call_id" in data, "tool_start.data 必须含 call_id（snake_case）"
        assert data["tool_name"] == "bash_execute"
        assert data["call_id"] == "call-abc-001"
        # 不应混入 camelCase（防止误改）
        assert "toolName" not in data, "tool_start 不应含 camelCase 的 toolName"
        assert "callId" not in data, "tool_start 不应含 camelCase 的 callId"

    def test_tool_result_uses_snake_case(self, bridge: PipelineStreamBridge, sink: _EventSink) -> None:
        """tool_result 事件 data 含 tool_name / call_id（snake_case）。"""
        # 先发 tool_start（tool_result 依赖它，否则触发 fixup 补发）
        _run(bridge._handle_chunk({
            "type": "tool_start", "tool_name": "file_read",
            "call_id": "call-def-002", "args": None,
        }))
        sink.events.clear()

        _run(bridge._handle_chunk({
            "type": "tool_result", "tool_name": "file_read",
            "call_id": "call-def-002", "success": True,
            "result": "file content", "duration_ms": 42,
        }))

        results = [e for e in sink.events if e["type"] == "tool_result"]
        assert len(results) == 1, f"应发一个 tool_result，得到 {len(results)}"

        data = results[0]["data"]
        assert "tool_name" in data
        assert "call_id" in data
        assert data["tool_name"] == "file_read"
        assert data["call_id"] == "call-def-002"
        assert "toolName" not in data
        assert "callId" not in data


# ============================================================
# parts[] 契约：tool_call 子项 = camelCase
# ============================================================


class TestPartsContract:
    """parts[] 的 tool_call 子项字段必须是 camelCase。

    数据源：bridge_core._build_parts_from_state。
    前端用 parts[].callId / parts[].name 渲染工具调用气泡。
    """

    def test_tool_call_part_uses_camel_case(self, bridge: PipelineStreamBridge) -> None:
        """_build_parts_from_state 生成的 tool_call part 含 name / callId（camelCase）。"""
        state = {
            "raw_tool_calls": [
                {"id": "call-xyz-003", "name": "bash_execute", "args": {"command": "ls"}},
            ],
            "tool_results": [
                {"call_id": "call-xyz-003", "data": "file1\nfile2", "success": True},
            ],
        }
        parts = bridge._build_parts_from_state(state)

        tool_call_parts = [p for p in parts if p.get("type") == "tool_call"]
        assert len(tool_call_parts) == 1, f"应有一个 tool_call part，得到 {len(tool_call_parts)}"

        part = tool_call_parts[0]
        # camelCase 契约
        assert "callId" in part, "tool_call part 必须含 callId（camelCase）"
        assert "name" in part, "tool_call part 必须含 name（camelCase）"
        assert part["callId"] == "call-xyz-003"
        assert part["name"] == "bash_execute"
        # 不应混入 snake_case（防止误改）
        assert "call_id" not in part, "parts[] tool_call 不应含 snake_case 的 call_id"
        assert "tool_name" not in part, "parts[] tool_call 不应含 snake_case 的 tool_name"

    def test_tool_call_part_carries_result_and_state(self, bridge: PipelineStreamBridge) -> None:
        """tool_call part 含 result / state（关联 tool_results）。"""
        state = {
            "raw_tool_calls": [
                {"id": "c1", "name": "file_read", "args": None},
            ],
            "tool_results": [
                {"call_id": "c1", "data": "content", "success": True},
            ],
        }
        parts = bridge._build_parts_from_state(state)
        tc = [p for p in parts if p.get("type") == "tool_call"][0]
        assert tc["result"] == "content"
        assert tc["state"] == "done"

    def test_failed_tool_call_state_is_error(self, bridge: PipelineStreamBridge) -> None:
        """失败的工具调用，part 的 state 应为 error。"""
        state = {
            "raw_tool_calls": [{"id": "c2", "name": "bash", "args": None}],
            "tool_results": [{"call_id": "c2", "data": "err", "success": False}],
        }
        parts = bridge._build_parts_from_state(state)
        tc = [p for p in parts if p.get("type") == "tool_call"][0]
        assert tc["state"] == "error"


# ============================================================
# 契约一致性：两个数据源的字段不交叉污染
# ============================================================


class TestContractConsistency:
    """验证两个数据源的字段命名不交叉污染（snake 与 camel 各守各的）。"""

    def test_realtime_and_parts_use_different_naming(
        self, bridge: PipelineStreamBridge, sink: _EventSink,
    ) -> None:
        """同一工具调用：实时事件 snake_case，parts[] camelCase，互不混入。"""
        # 触发实时事件
        _run(bridge._handle_chunk({
            "type": "tool_start", "tool_name": "bash",
            "call_id": "shared-id", "args": None,
        }))
        # 构造对应 state 生成 parts
        state = {
            "raw_tool_calls": [{"id": "shared-id", "name": "bash", "args": None}],
            "tool_results": [],
        }
        parts = bridge._build_parts_from_state(state)

        # 实时事件 = snake
        rt_data = [e for e in sink.events if e["type"] == "tool_start"][0]["data"]
        assert set(["tool_name", "call_id"]).issubset(rt_data.keys())
        assert "callId" not in rt_data

        # parts[] = camel
        tc_part = [p for p in parts if p.get("type") == "tool_call"][0]
        assert set(["name", "callId"]).issubset(tc_part.keys())
        assert "call_id" not in tc_part


# ============================================================
# 多轮顺序契约：message_id 变化 + 状态重置（来自 e2e_ws_multiturn_order）
# ============================================================


class TestMultiTurnOrder:
    """多轮对话的事件顺序 + message_id 隔离（来自 e2e_ws_multiturn_order）。

    验证点：
    - 每轮 stream_start 带独立的 message_id（reset_for_new_turn 换 id）
    - 第二轮的 tool_start 不被第一轮的 dedup 集合误杀（_sent_tool_starts 已重置）
    - chunk 不会串到上一轮（_stream_started 状态正确）
    """

    def test_each_turn_has_distinct_message_id(
        self, sink: _EventSink,
    ) -> None:
        """两轮对话，stream_start 的 message_id 必须不同（每轮独立 hex id）。

        emit_start 内部 _start_new_turn 无条件重新生成 hex message_id，
        不沿用构造/reset 时传入的值。本测试验证多轮 id 隔离。
        """
        b = PipelineStreamBridge("pipe-mt", sink, message_id="msg-turn-1")
        _run(b.emit_start())
        # 第一轮结束（emit_finish 推 new_message + stream_end）
        _run(b.emit_finish({"raw_result": "第一轮回复"}))

        # 第二轮
        _run(b.emit_start())
        _run(b.emit_finish({"raw_result": "第二轮回复"}))

        starts = [e for e in sink.events if e["type"] == "stream_start"]
        assert len(starts) == 2, f"应有 2 个 stream_start，得到 {len(starts)}"

        ids = [s["data"]["message_id"] for s in starts]
        assert ids[0] != ids[1], (
            f"两轮 message_id 必须不同（多轮隔离），得到 {ids}"
        )
        # message_id 应为 hex 格式（12 位），非构造时传入的字面值
        for mid in ids:
            assert len(mid) == 12 and all(c in "0123456789abcdef" for c in mid), (
                f"message_id 应为 12 位 hex，得到 {mid}"
            )

    def test_tool_start_dedup_resets_across_turns(
        self, sink: _EventSink,
    ) -> None:
        """第二轮同 call_id 的 tool_start 应能再次发送（dedup 集合已重置）。

        若 reset_for_new_turn 漏清 _sent_tool_starts，第二轮的 tool_start 会被
        当作重复丢弃，前端表现为"第二轮工具调用不显示"。
        """
        b = PipelineStreamBridge("pipe-dedup", sink, message_id="msg-d-1")
        _run(b.emit_start())
        # 第一轮发 tool_start(call_id=c1)
        _run(b._handle_chunk({
            "type": "tool_start", "tool_name": "bash",
            "call_id": "c1", "args": None,
        }))
        first_starts = [e for e in sink.events if e["type"] == "tool_start"]
        assert len(first_starts) == 1, "第一轮应发一个 tool_start"

        # 第二轮：reset 后同 call_id 应能再发
        b.reset_for_new_turn(message_id="msg-d-2")
        _run(b.emit_start())
        _run(b._handle_chunk({
            "type": "tool_start", "tool_name": "bash",
            "call_id": "c1", "args": None,
        }))

        all_starts = [e for e in sink.events if e["type"] == "tool_start"]
        assert len(all_starts) == 2, (
            f"两轮各发一个 tool_start 应共 2 个，得到 {len(all_starts)}（dedup 未重置？）"
        )

    def test_chunk_before_start_in_new_turn_dropped(
        self, sink: _EventSink,
    ) -> None:
        """第二轮 reset 后未 emit_start 就发 chunk，应被丢弃（_stream_started=False）。"""
        b = PipelineStreamBridge("pipe-guard", sink, message_id="msg-g-1")
        _run(b.emit_start())
        _run(b.emit_finish({"raw_result": "done"}))

        # reset 后 _stream_started 应为 False，直接发 chunk 应被丢弃
        b.reset_for_new_turn(message_id="msg-g-2")
        sink.events.clear()
        _run(b.emit_chunk({"type": "text", "content": "不应到达"}))

        assert len(sink.events) == 0, (
            f"reset 后未 emit_start 的 chunk 应被丢弃，得到 {len(sink.events)} 个事件"
        )
