"""测试 LLM 调用超时保护机制。

验证 BUG-FIX-fix_20260506_llm_timeout：当 engine.run() 挂起时，
asyncio.wait_for 超时保护能正确触发，发送错误消息给前端。
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 测试 _get_call_timeout
# ---------------------------------------------------------------------------


def test_get_call_timeout_returns_default_when_config_unavailable():
    """配置加载失败时返回默认值 120 秒。"""
    from stream_handler import _get_call_timeout
    import stream_handler

    stream_handler._cached_call_timeout = None
    with patch.dict("sys.modules", {}):
        timeout = _get_call_timeout()
    assert timeout == 120
    stream_handler._cached_call_timeout = None


def test_get_call_timeout_caches_result():
    """_get_call_timeout 只加载一次配置，后续返回缓存值。"""
    import stream_handler

    stream_handler._cached_call_timeout = 300
    from stream_handler import _get_call_timeout

    assert _get_call_timeout() == 300
    stream_handler._cached_call_timeout = None


# ---------------------------------------------------------------------------
# 辅助：轻量级 Fake 对象，避免 MagicMock 属性链问题
# ---------------------------------------------------------------------------


class _FakeNotifier:
    def __init__(self, sent_messages):
        self._sent = sent_messages

    async def send_to_thread(self, thread_id, event):
        if isinstance(event, dict):
            self._sent.append(event)
        return True


class _FakeEngine:
    """模拟 PipelineEngine，使用真实 async 函数替代 MagicMock。"""

    def __init__(self, run_fn):
        self.pipeline_id = "test-pipeline"
        self._run_fn = run_fn

    async def run(self, **kwargs):
        return await self._run_fn(**kwargs)


class _FakeCtx:
    """模拟 PipelineContext，提供 handle_stream_request 所需接口。"""

    def __init__(self, engine):
        self.engine = engine
        self.agent_config = MagicMock()
        self.services: dict = {}

    def get_or_create_engine(self, pipeline_id: str):
        return self.engine


def _build_stream_context(
    websocket,
    user_content="test",
    message_id="test-msg-id",
    stop_event=None,
    thread_id="test-thread",
    conversation_history=None,
    pipeline_ctx=None,
    ws_notifier=None,
    sent_messages=None,
):
    """构造 StreamContext 用于测试。"""
    from stream_handler import StreamContext

    if stop_event is None:
        stop_event = asyncio.Event()
    if conversation_history is None:
        conversation_history = [{"role": "user", "content": "test", "id": "u1"}]
    if ws_notifier is None and sent_messages is not None:
        ws_notifier = _FakeNotifier(sent_messages)
    return StreamContext(
        pipeline_id="",
        message_id=message_id,
        thread_id=thread_id,
        conversation_history=conversation_history,
        ws_notifier=ws_notifier,
        websocket=websocket,
        stop_event=stop_event,
        user_content=user_content,
        pipeline_ctx=pipeline_ctx,
    )


# ---------------------------------------------------------------------------
# 测试超时保护核心逻辑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_timeout_sends_error_to_frontend():
    """engine.run() 挂起超过 call_timeout 时，前端收到 stream_end 超时事件。"""
    from stream_handler import handle_stream_request
    import stream_handler

    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    async def hanging_run(**kwargs):
        await asyncio.sleep(9999)

    fake_engine = _FakeEngine(hanging_run)
    fake_ctx = _FakeCtx(fake_engine)

    stream_handler._cached_call_timeout = 1

    websocket = FakeWebSocket()
    stop_event = asyncio.Event()

    sctx = _build_stream_context(
        websocket=websocket,
        stop_event=stop_event,
        pipeline_ctx=fake_ctx,
        sent_messages=sent_messages,
    )
    with pytest.raises(TimeoutError):
        await handle_stream_request(sctx)

    stream_handler._cached_call_timeout = None

    stream_ends = [m for m in sent_messages if m.get("type") == "stream_end"]
    assert len(stream_ends) >= 1
    assert stream_ends[0]["data"].get("timed_out") is True


@pytest.mark.asyncio
async def test_drain_timeout_sends_stream_end_when_stream_started():
    """超时时如果 stream 已开始，前端应收到 stream_end。"""
    from stream_handler import handle_stream_request
    import stream_handler

    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    async def partial_then_hang(**kwargs):
        on_chunk_cb = kwargs.get("on_chunk")
        if on_chunk_cb:
            on_chunk_cb({"type": "text", "content": "开始回复..."})
        await asyncio.sleep(9999)

    fake_engine = _FakeEngine(partial_then_hang)
    fake_ctx = _FakeCtx(fake_engine)

    stream_handler._cached_call_timeout = 1

    websocket = FakeWebSocket()
    stop_event = asyncio.Event()

    sctx = _build_stream_context(
        websocket=websocket,
        stop_event=stop_event,
        pipeline_ctx=fake_ctx,
        sent_messages=sent_messages,
    )
    with pytest.raises(TimeoutError):
        await handle_stream_request(sctx)

    stream_handler._cached_call_timeout = None

    stream_ends = [m for m in sent_messages if m.get("type") == "stream_end"]
    assert len(stream_ends) >= 1


@pytest.mark.asyncio
async def test_normal_flow_not_affected_by_timeout():
    """正常流程（engine.run 快速完成）不受超时保护影响。"""
    from stream_handler import handle_stream_request
    import stream_handler

    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    async def quick_run(**kwargs):
        on_chunk_cb = kwargs.get("on_chunk")
        if on_chunk_cb:
            on_chunk_cb({"type": "text", "content": "正常回复"})
        return {"messages": [], "raw_result": "正常回复内容"}

    fake_engine = _FakeEngine(quick_run)
    fake_ctx = _FakeCtx(fake_engine)

    stream_handler._cached_call_timeout = 120

    websocket = FakeWebSocket()
    stop_event = asyncio.Event()

    sctx = _build_stream_context(
        websocket=websocket,
        stop_event=stop_event,
        pipeline_ctx=fake_ctx,
        sent_messages=sent_messages,
    )
    await handle_stream_request(sctx)

    stream_ends = [m for m in sent_messages if m.get("type") == "stream_end"]
    new_msgs = [m for m in sent_messages if m.get("type") == "new_message"]
    assert len(stream_ends) >= 1
    assert len(new_msgs) >= 1

    stream_handler._cached_call_timeout = None
