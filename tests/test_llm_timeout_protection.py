"""测试 LLM 调用超时保护机制。

验证 BUG-FIX-fix_20260506_llm_timeout：当 LLM 长时间无响应时，
PipelineStreamBridge.drain_loop 的 call_timeout 超时保护能正确触发，
发送 stream_end(timed_out=True) 事件给前端。

新架构中超时保护在 drain_loop 内部实现（stream_bridge.py），
通过 call_timeout 参数控制 LLM 活动超时检测。
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# 测试 _get_call_timeout
# ---------------------------------------------------------------------------


def test_get_call_timeout_returns_default_when_config_unavailable():
    """配置加载失败时返回默认值 120 秒。"""
    from channels.websocket.stream_handler import _get_call_timeout
    from channels.websocket import stream_handler

    stream_handler._cached_call_timeout = None
    with patch.dict("sys.modules", {}):
        timeout = _get_call_timeout()
    assert timeout == 120
    stream_handler._cached_call_timeout = None


def test_get_call_timeout_caches_result():
    """_get_call_timeout 只加载一次配置，后续返回缓存值。"""
    from channels.websocket import stream_handler

    stream_handler._cached_call_timeout = 300
    from channels.websocket.stream_handler import _get_call_timeout

    assert _get_call_timeout() == 300
    stream_handler._cached_call_timeout = None


# ---------------------------------------------------------------------------
# 辅助：轻量级 Fake Sink，收集 drain_loop 发送的所有事件
# ---------------------------------------------------------------------------


class _FakeSink:
    """模拟 IOutputSink，收集发送的事件到列表中供断言检查。"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    @property
    def sink_id(self) -> str:
        """返回测试用 sink 标识。"""
        return "fake:test-sink"

    async def send_event(self, event: dict) -> bool:
        """记录发送的事件，始终返回成功。"""
        self.events.append(event)
        return True


# ---------------------------------------------------------------------------
# 测试超时保护核心逻辑（drain_loop + call_timeout）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_timeout_sends_error_to_frontend():
    """drain_loop 在 LLM 无活动超过 call_timeout 时发送 stream_end(timed_out=True)。

    模拟场景：engine_task 一直运行但 bridge 队列无新 chunk，
    drain_loop 超时退出并通知前端。
    """
    from pipeline.stream_bridge import PipelineStreamBridge

    sink = _FakeSink()
    bridge = PipelineStreamBridge(
        pipeline_id="test-timeout-pipeline",
        output_sink=sink,
        message_id="test-timeout-msg",
    )

    # 创建一个永不结束的 engine_task（模拟 LLM 挂起）
    async def _hang_forever():
        await asyncio.sleep(9999)

    engine_task = asyncio.create_task(_hang_forever())

    try:
        # call_timeout=1 秒，drain_loop 应在约 1 秒后超时退出
        result = await bridge.drain_loop(
            engine_task,
            heartbeat_interval=5.0,
            call_timeout=1,
        )

        # 验证 drain_loop 返回结果标记超时
        assert result.get("timed_out") is True

        # 验证前端收到了 stream_end 事件且标记 timed_out
        stream_ends = [e for e in sink.events if e.get("type") == "stream_end"]
        assert len(stream_ends) >= 1
        assert stream_ends[0]["data"].get("timed_out") is True
    finally:
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_drain_timeout_sends_stream_end_when_stream_started():
    """超时时如果 stream 已开始（已有 chunk 输出），前端仍应收到 stream_end。

    模拟场景：LLM 输出部分内容后挂起，drain_loop 超时退出，
    前端应收到包含已累积内容的 stream_end 事件。
    """
    from pipeline.stream_bridge import PipelineStreamBridge

    sink = _FakeSink()
    bridge = PipelineStreamBridge(
        pipeline_id="test-partial-pipeline",
        output_sink=sink,
        message_id="test-partial-msg",
    )

    # 模拟部分输出后挂起：先注入 chunk，然后永不结束
    async def _partial_then_hang():
        bridge.on_chunk({"type": "text", "content": "开始回复..."})
        await asyncio.sleep(9999)

    engine_task = asyncio.create_task(_partial_then_hang())

    try:
        # call_timeout=1 秒
        result = await bridge.drain_loop(
            engine_task,
            heartbeat_interval=5.0,
            call_timeout=1,
        )

        # 验证 drain_loop 返回结果标记超时且包含已累积内容
        assert result.get("timed_out") is True
        assert "开始回复..." in result.get("accumulated_content", "")

        # 验证前端收到了 stream_end 事件
        stream_ends = [e for e in sink.events if e.get("type") == "stream_end"]
        assert len(stream_ends) >= 1
    finally:
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_normal_flow_not_affected_by_timeout():
    """正常流程（engine_task 快速完成并输出内容）不受 call_timeout 影响。

    模拟场景：LLM 正常输出内容并完成，drain_loop 正常退出，
    stream_end 不包含 timed_out 标记。
    """
    from pipeline.stream_bridge import PipelineStreamBridge

    sink = _FakeSink()
    bridge = PipelineStreamBridge(
        pipeline_id="test-normal-pipeline",
        output_sink=sink,
        message_id="test-normal-msg",
    )

    # 模拟正常 LLM 流程：输出内容后完成
    async def _quick_complete():
        bridge.on_chunk({"type": "text", "content": "正常回复内容"})
        # engine_task 结束后 drain_loop 应正常退出

    engine_task = asyncio.create_task(_quick_complete())

    try:
        # call_timeout=120 秒（远大于测试时间，不应触发）
        result = await bridge.drain_loop(
            engine_task,
            heartbeat_interval=5.0,
            call_timeout=120,
        )

        # 验证 drain_loop 正常完成，未超时
        assert result.get("timed_out") is False
        assert "正常回复内容" in result.get("accumulated_content", "")

        # 验证前端收到了 stream_end 事件且未标记超时
        stream_ends = [e for e in sink.events if e.get("type") == "stream_end"]
        assert len(stream_ends) >= 1
        assert stream_ends[0]["data"].get("timed_out") is not True
    finally:
        engine_task.cancel()
        try:
            await engine_task
        except asyncio.CancelledError:
            pass
