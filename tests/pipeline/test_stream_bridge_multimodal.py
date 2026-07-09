"""管道流式桥接器 — 工具多模态结果回流测试

覆盖场景：
- tool_multimedia_result chunk 格式化
- _handle_chunk 正确处理各种 chunk 类型
- emit_finish / emit_start / emit_error 流程
- bridge 状态管理（thinking 开启/关闭、dedup）
- _build_parts_from_state 重建 parts
- _make_event 自动注入追踪字段
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from pipeline.stream_bridge import PipelineStreamBridge
from pipeline.sink import IOutputSink


class _EventSink(IOutputSink):
    """Mock Sink：记录所有推送事件。"""
    def __init__(self):
        self.events: list[dict] = []

    async def send_event(self, event: dict) -> bool:
        self.events.append(event)
        return True

    @property
    def sink_id(self) -> str:
        return "test-sink"


def _run(coro):
    """安全执行 async 函数。"""
    try:
        asyncio.get_running_loop()
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result(timeout=10)
    except RuntimeError:
        return asyncio.run(coro)


@pytest.fixture
def sink():
    return _EventSink()


@pytest.fixture
def bridge(sink):
    return PipelineStreamBridge("pipe-001", sink, message_id="msg-001")


# ============================================================
# tool_multimedia_result chunk — 核心场景
# ============================================================

class TestToolMultimediaResult:
    """tool_multimedia_result chunk 格式化。"""

    def test_multimedia_result_with_images(self, bridge, sink):
        """含多张图片的结果正确格式化。"""
        chunk = {
            "type": "tool_multimedia_result",
            "count": 2,
            "multimedia": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/"}},
            ],
        }
        _run(bridge._handle_chunk(chunk))
        evt = sink.events[0]
        assert evt["type"] == "tool_multimedia_result"
        assert evt["data"]["count"] == 2
        assert len(evt["data"]["multimedia"]) == 2
        assert "sequence" in evt["data"]
        assert evt["data"]["pipeline_id"] == "pipe-001"

    def test_multimedia_result_empty(self, bridge, sink):
        """空多模态结果可安全处理。"""
        chunk = {"type": "tool_multimedia_result", "count": 0, "multimedia": []}
        _run(bridge._handle_chunk(chunk))
        evt = sink.events[0]
        assert evt["data"]["count"] == 0
        assert evt["data"]["multimedia"] == []

    def test_multimedia_result_single(self, bridge, sink):
        """单个多模态结果正确推送。"""
        chunk = {
            "type": "tool_multimedia_result",
            "count": 1,
            "multimedia": [{"type": "image", "source": {"type": "base64", "data": "AAA", "media_type": "image/png"}}],
        }
        _run(bridge._handle_chunk(chunk))
        evt = sink.events[0]
        assert evt["data"]["count"] == 1
        assert evt["data"]["multimedia"][0]["type"] == "image"


# ============================================================
# 各 chunk 类型格式化
# ============================================================

class TestChunkTypes:
    """_handle_chunk 各类型格式化。"""

    def test_text_chunk(self, bridge, sink):
        """text → stream_chunk。"""
        _run(bridge._handle_chunk({"type": "text", "content": "Hello"}))
        assert sink.events[0]["type"] == "stream_chunk"
        assert sink.events[0]["data"]["content"] == "Hello"

    def test_thinking_start_and_chunk(self, bridge, sink):
        """thinking → thinking_start + thinking_chunk。"""
        _run(bridge._handle_chunk({"type": "thinking", "content": "Hmm..."}))
        types = {e["type"] for e in sink.events}
        assert "thinking_start" in types
        assert "thinking_chunk" in types

    def test_thinking_end_closes(self, bridge, sink):
        """thinking_end 关闭 thinking 状态。"""
        bridge._thinking_active = True
        _run(bridge._handle_chunk({"type": "thinking_end", "duration_ms": 500}))
        assert bridge._thinking_active is False
        assert sink.events[0]["type"] == "thinking_end"

    def test_tool_start_dedup(self, bridge, sink):
        """同 call_id 只发一次 tool_start。"""
        ch = {"type": "tool_start", "tool_name": "bash", "call_id": "c1"}
        _run(bridge._handle_chunk(ch))
        _run(bridge._handle_chunk(ch))
        starts = [e for e in sink.events if e["type"] == "tool_start"]
        assert len(starts) == 1

    def test_tool_result_fixup(self, bridge, sink):
        """tool_result 自动补发 tool_start。"""
        _run(bridge._handle_chunk({
            "type": "tool_result", "tool_name": "read", "call_id": "c2",
            "success": True, "result": "ok",
        }))
        types = {e["type"] for e in sink.events}
        assert "tool_start" in types
        assert "tool_result" in types

    def test_iteration_chunk(self, bridge, sink):
        """iteration → iteration 事件。"""
        _run(bridge._handle_chunk({"type": "iteration", "iteration": 5, "max_iterations": 100}))
        assert sink.events[0]["type"] == "iteration"

    def test_notification_chunk(self, bridge, sink):
        """notification → system_notification。"""
        _run(bridge._handle_chunk({
            "type": "notification", "content": "超时", "level": "warn",
            "notificationType": "timeout", "notification_id": "n1", "sequence": 1,
        }))
        assert sink.events[0]["type"] == "system_notification"


# ============================================================
# emit 流程
# ============================================================

class TestEmitFlow:
    """emit_start / finish / error / suspend / chunk / notification。"""

    def test_emit_start(self, sink):
        """emit_start 推送 stream_start。"""
        b = PipelineStreamBridge("p1", sink)
        _run(b.emit_start())
        assert any(e["type"] == "stream_start" for e in sink.events)

    def test_emit_finish(self, sink):
        """emit_finish 推送 new_message + stream_end。"""
        b = PipelineStreamBridge("p2", sink)
        b._stream_started = True
        b._emit_start_time = 1000.0
        _run(b.emit_finish({"raw_result": "完成"}))
        types = {e["type"] for e in sink.events}
        assert "new_message" in types
        assert "stream_end" in types

    def test_emit_finish_empty_skips_new_message(self, sink):
        """空内容跳过 new_message，只发 stream_end。"""
        b = PipelineStreamBridge("p3", sink)
        b._stream_started = True
        _run(b.emit_finish({"raw_result": ""}))
        types = {e["type"] for e in sink.events}
        assert "new_message" not in types
        assert "stream_end" in types

    def test_emit_error(self, sink):
        """emit_error 推送 stream_error。"""
        b = PipelineStreamBridge("p4", sink)
        b._stream_started = True
        _run(b.emit_error(RuntimeError("崩了")))
        assert any("崩了" in e["data"].get("error", "") for e in sink.events)

    def test_emit_suspend(self, sink):
        """emit_suspend 推送 state_change + stream_end。"""
        b = PipelineStreamBridge("p5", sink)
        b._stream_started = True
        _run(b.emit_suspend({"raw_result": "部分"}))
        types = {e["type"] for e in sink.events}
        assert "state_change" in types
        assert "stream_end" in types

    def test_emit_chunk_before_start_dropped(self, sink):
        """未 start 时 chunk 被丢弃。"""
        b = PipelineStreamBridge("p6", sink)
        b._stream_started = False
        _run(b.emit_chunk({"type": "text", "content": "早到"}))
        assert len(sink.events) == 0

    def test_emit_notification(self, sink):
        """emit_notification 推送 system_notification，返回 record_id（唯一 id 来源）。"""
        b = PipelineStreamBridge("p7", sink)
        record_id = _run(b.emit_notification("任务完成"))
        # 返回 hex12 record_id（与 AI message_id 同格式，非 int seq）
        assert isinstance(record_id, str) and len(record_id) == 12
        data = sink.events[0]["data"]
        assert data["content"] == "任务完成"
        # record_id 必须出现在 payload（前端据此设消息 id，与 track 落库对齐）
        assert data["record_id"] == record_id

    def test_emit_notification_empty_returns_empty(self, sink):
        """空通知返回空串（拒绝推送）。"""
        b = PipelineStreamBridge("p8", sink)
        assert _run(b.emit_notification("  ")) == ""


# ============================================================
# _make_event / 状态管理
# ============================================================

class TestMakeEvent:
    """_make_event 自动注入字段。"""

    def test_injects_pipeline_and_message_id(self, bridge):
        evt = bridge._make_event("x", {"a": 1})
        assert evt["data"]["pipeline_id"] == "pipe-001"
        assert evt["data"]["message_id"] == "msg-001"

    def test_preserves_custom_fields(self, bridge):
        evt = bridge._make_event("x", {"custom": "v", "pipeline_id": "override"})
        assert evt["data"]["custom"] == "v"
        assert evt["data"]["pipeline_id"] == "override"  # setdefault 不覆盖


class TestBuildParts:
    """_build_parts_from_state。"""

    def test_thinking_part(self, bridge):
        parts = bridge._build_parts_from_state({"raw_thinking": "思考"})
        assert any(p["type"] == "thinking" and p["content"] == "思考" for p in parts)

    def test_tool_call_part(self, bridge):
        state = {
            "raw_tool_calls": [{"id": "c1", "name": "read", "args": {"p": "/x"}}],
            "tool_results": [{"call_id": "c1", "data": "ok", "success": True}],
        }
        parts = bridge._build_parts_from_state(state)
        tc = [p for p in parts if p["type"] == "tool_call"]
        assert len(tc) == 1
        assert tc[0]["state"] == "done"

    def test_text_part(self, bridge):
        parts = bridge._build_parts_from_state({"raw_result": "回复"})
        assert any(p["type"] == "text" for p in parts)

    def test_empty_state(self, bridge):
        assert bridge._build_parts_from_state({}) == []


class TestBridgeCore:
    """BridgeCore 基础"""

    def test_auto_generate_message_id(self, sink):
        b = PipelineStreamBridge("auto", sink)
        assert len(b.message_id) == 12

    def test_reset_for_new_turn(self, sink):
        b = PipelineStreamBridge("rst", sink)
        b._thinking_active = True
        b._stream_started = True
        b.reset_for_new_turn("custom")
        assert b.message_id == "custom"
        assert b._stream_started is False
        assert b._thinking_active is False

    def test_send_event_public(self, sink):
        b = PipelineStreamBridge("pub", sink)
        assert _run(b.send_event({"type": "t", "data": {}})) is True
        assert len(sink.events) == 1
