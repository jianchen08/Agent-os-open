"""Regression test for BUG-FIX-fix_20260615_swallowed_business_error.

When retries are exhausted (business error), engine.run() returns normally
(exception() is None) but state contains raw_error/error_analysis.
_finalize_drain must send stream_error (not empty stream_end) so the frontend
shows the real error.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.stream_bridge import PipelineStreamBridge


def _make_bridge() -> PipelineStreamBridge:
    """Build a bridge with a mock sink; stream_started already True."""
    sink = MagicMock()
    sink.send_event = AsyncMock(return_value=True)
    bridge = PipelineStreamBridge(
        pipeline_id="test_pipe", output_sink=sink, message_id="msg_test",
    )
    bridge._stream_started = True
    return bridge


async def _make_done_task(state: dict) -> asyncio.Task:
    """Build a completed task: result() returns state, exception() is None."""
    async def _run() -> dict:
        return state
    task = asyncio.create_task(_run())
    await task
    return task


def _sent_event_types(bridge: PipelineStreamBridge) -> list[str]:
    return [c.args[0]["type"] for c in bridge.output_sink.send_event.call_args_list]


@pytest.mark.asyncio
async def test_finalize_drain_sends_stream_error_on_business_error():
    """state with raw_error should trigger stream_error."""
    bridge = _make_bridge()
    engine_task = await _make_done_task(
        {"raw_error": "APIConnectionError: connect refused"}
    )

    await bridge._finalize_drain(engine_task, None, False, 0)

    types = _sent_event_types(bridge)
    assert "stream_error" in types, f"expected stream_error, got {types}"
    assert "stream_end" not in types, "should not send empty stream_end"


@pytest.mark.asyncio
async def test_finalize_drain_prefers_error_analysis_reason():
    """error_analysis.reason takes precedence over raw_error."""
    bridge = _make_bridge()
    engine_task = await _make_done_task({
        "raw_error": "raw msg",
        "error_analysis": {"reason": "Retryable core_error (3/3)"},
    })

    await bridge._finalize_drain(engine_task, None, False, 0)

    err_events = [
        c.args[0] for c in bridge.output_sink.send_event.call_args_list
        if c.args[0]["type"] == "stream_error"
    ]
    assert err_events, "should send stream_error"
    assert "core_error" in err_events[0]["data"]["error"]


@pytest.mark.asyncio
async def test_finalize_drain_no_error_falls_through_to_stream_end():
    """state without error must not send stream_error; falls through to stream_end."""
    bridge = _make_bridge()
    engine_task = await _make_done_task({"raw_error": None, "error_analysis": None})

    await bridge._finalize_drain(engine_task, None, False, 0)

    types = _sent_event_types(bridge)
    assert "stream_error" not in types, "should not send stream_error when no error"
    assert "stream_end" in types, "should send stream_end when no content"
