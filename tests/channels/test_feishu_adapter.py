"""飞书适配器测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from channels.feishu.adapter import FeishuAdapter, FeishuInputAdapter, FeishuOutputAdapter
from channels.feishu.stream_client import FeishuStreamClient


class TestFeishuInputAdapter:
    """FeishuInputAdapter 测试。"""

    def test_init(self) -> None:
        """测试初始化。"""
        adapter = FeishuInputAdapter()
        assert adapter is not None
        assert hasattr(adapter, "_message_queue")

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """测试消息入队和接收。"""
        adapter = FeishuInputAdapter()
        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello"}',
                    "create_time": "1700000000000",
                },
            },
        }
        await adapter.enqueue_message(raw_msg)
        state = await adapter.receive()
        assert state["user_input"] == "hello"
        assert state["_channel_type"] == "feishu"


class TestFeishuOutputAdapter:
    """FeishuOutputAdapter 测试。"""

    @pytest.mark.asyncio
    async def test_send_text(self) -> None:
        """测试发送文本消息。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello result",
            "_channel_user_id": "ou_test",
            "ended": True,
        }
        await adapter.send(state)
        client.send_message.assert_called_once()
        call_args = client.send_message.call_args
        assert call_args[0][0] == "ou_test"
        assert call_args[0][1] == "Hello result"

    @pytest.mark.asyncio
    async def test_send_stream(self) -> None:
        """测试流式发送。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_test")

        chunk = {"text": "Hello ", "type": "token"}
        await adapter.send_stream(chunk)
        # 流式消息应累积并发送
        assert adapter._accumulated_text == "Hello "

    @pytest.mark.asyncio
    async def test_send_with_error(self) -> None:
        """测试发送错误消息。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_error": "Something went wrong",
            "_channel_user_id": "ou_test",
        }
        await adapter.send(state)
        client.send_message.assert_called_once()
        assert "Something went wrong" in client.send_message.call_args[0][1]


class TestFeishuAdapter:
    """FeishuAdapter 组合模式测试。"""

    def test_init(self) -> None:
        """测试初始化。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.stream_client is not None

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """测试启动适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.connect = AsyncMock()
        adapter.stream_client.start_receive_loop = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """测试停止适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_called_once()

    def test_channel_type(self) -> None:
        """测试通道类型。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.channel_type == "feishu"
