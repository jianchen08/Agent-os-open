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

    # 重置缓存
    stream_handler._cached_call_timeout = None
    with patch.dict("sys.modules", {}):
        timeout = _get_call_timeout()
    assert timeout == 120
    # 恢复缓存
    stream_handler._cached_call_timeout = None


def test_get_call_timeout_caches_result():
    """_get_call_timeout 只加载一次配置，后续返回缓存值。"""
    import stream_handler

    stream_handler._cached_call_timeout = 300
    from stream_handler import _get_call_timeout

    assert _get_call_timeout() == 300
    # 恢复
    stream_handler._cached_call_timeout = None


# ---------------------------------------------------------------------------
# 辅助：轻量级 Fake 对象，避免 MagicMock 属性链问题
# ---------------------------------------------------------------------------


class _FakeEngine:
    """模拟 PipelineEngine，使用真实 async 函数替代 MagicMock。"""

    def __init__(self, run_fn):
        self.pipeline_id = "test-pipeline"
        self._run_fn = run_fn

    async def run(self, **kwargs):
        return await self._run_fn(**kwargs)


class _FakeCtx:
    """模拟 PipelineContext，提供 _stream_engine_response 所需接口。"""

    def __init__(self, engine):
        self.engine = engine
        self.agent_config = MagicMock()
        self.services: dict = {}

    def get_or_create_engine(self, pipeline_id: str):
        return self.engine


# ---------------------------------------------------------------------------
# 测试超时保护核心逻辑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_timeout_sends_error_to_frontend():
    """engine.run() 挂起超过 call_timeout 时，前端收到 new_message 错误消息。"""
    from stream_handler import _stream_engine_response
    import stream_handler

    # 模拟 WebSocket
    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    # 模拟一个永远不会完成的 engine task
    async def hanging_run(**kwargs):
        await asyncio.sleep(9999)

    fake_engine = _FakeEngine(hanging_run)
    fake_ctx = _FakeCtx(fake_engine)

    # 设置极短超时以加速测试
    stream_handler._cached_call_timeout = 1

    websocket = FakeWebSocket()
    stop_event = asyncio.Event()

    # 函数内部 catch 了 TimeoutError，不会向外抛出
    await _stream_engine_response(
        websocket=websocket,
        user_content="test",
        message_id="test-msg-id",
        stop_event=stop_event,
        thread_id="test-thread",
        conversation_history=[
            {"role": "user", "content": "test", "id": "u1"}
        ],
        ctx=fake_ctx,
    )

    stream_handler._cached_call_timeout = None

    # 验证前端收到了错误消息（new_message）
    new_msg = [m for m in sent_messages if m.get("type") == "new_message"]
    assert len(new_msg) >= 1
    assert "超时" in new_msg[0]["data"]["content"]


@pytest.mark.asyncio
async def test_drain_timeout_sends_stream_end_when_stream_started():
    """超时时如果 stream 已开始，前端应收到 stream_end。"""
    from stream_handler import _stream_engine_response
    import stream_handler

    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    # 模拟 engine.run：先触发一个 text chunk（启动 stream），然后挂起
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

    await _stream_engine_response(
        websocket=websocket,
        user_content="test",
        message_id="test-msg-id",
        stop_event=stop_event,
        thread_id="test-thread",
        conversation_history=[
            {"role": "user", "content": "test", "id": "u1"}
        ],
        ctx=fake_ctx,
    )

    stream_handler._cached_call_timeout = None

    # 验证：应该有 stream_end 消息（因为 stream_started 为 True）
    stream_ends = [m for m in sent_messages if m.get("type") == "stream_end"]
    assert len(stream_ends) >= 1
    # 验证：应该有 new_message 消息
    new_msgs = [m for m in sent_messages if m.get("type") == "new_message"]
    assert len(new_msgs) >= 1


@pytest.mark.asyncio
async def test_normal_flow_not_affected_by_timeout():
    """正常流程（engine.run 快速完成）不受超时保护影响。"""
    from stream_handler import _stream_engine_response
    import stream_handler

    sent_messages: list[dict] = []

    class FakeWebSocket:
        async def send_text(self, text: str):
            sent_messages.append(json.loads(text))

    # 模拟正常完成的 engine.run
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

    await _stream_engine_response(
        websocket=websocket,
        user_content="test",
        message_id="test-msg-id",
        stop_event=stop_event,
        thread_id="test-thread",
        conversation_history=[
            {"role": "user", "content": "test", "id": "u1"}
        ],
        ctx=fake_ctx,
    )

    # 验证正常流程：有 stream_end 和 new_message
    stream_ends = [m for m in sent_messages if m.get("type") == "stream_end"]
    new_msgs = [m for m in sent_messages if m.get("type") == "new_message"]
    assert len(stream_ends) >= 1
    assert len(new_msgs) >= 1

    stream_handler._cached_call_timeout = None
