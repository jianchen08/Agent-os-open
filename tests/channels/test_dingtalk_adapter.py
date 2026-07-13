"""钉钉适配器测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from channels.dingtalk.adapter import DingTalkAdapter, DingTalkInputAdapter, DingTalkOutputAdapter
from channels.dingtalk.stream_client import DingTalkStreamClient


class TestDingTalkInputAdapter:
    """DingTalkInputAdapter 测试。"""

    def test_init(self) -> None:
        """测试初始化。"""
        adapter = DingTalkInputAdapter()
        assert adapter is not None

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """测试消息入队和接收。"""
        adapter = DingTalkInputAdapter()
        raw_msg = {
            "conversationId": "cid-1",
            "senderStaffId": "user_dt_1",
            "senderId": "sender-1",
            "msgtype": "text",
            "text": {"content": "Hello DingTalk"},
            "createAt": "1700000000000",
            "messageId": "msg-dt-1",
        }
        await adapter.enqueue_message(raw_msg)
        state = await adapter.receive()
        assert state["user_input"] == "Hello DingTalk"
        assert state["_channel_type"] == "dingtalk"


class TestDingTalkOutputAdapter:
    """DingTalkOutputAdapter 测试。"""

    @pytest.mark.asyncio
    async def test_send_text(self) -> None:
        """测试发送文本消息。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello result",
            "_channel_user_id": "user_dt_1",
            "ended": True,
        }
        await adapter.send(state)
        client.send_message.assert_called_once()
        call_args = client.send_message.call_args
        assert call_args[0][0] == "user_dt_1"

    @pytest.mark.asyncio
    async def test_send_stream(self) -> None:
        """测试流式发送。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("user_dt_1")

        chunk = {"text": "Hello ", "type": "token"}
        await adapter.send_stream(chunk)
        assert adapter._accumulated_text == "Hello "


class TestDingTalkAdapter:
    """DingTalkAdapter 组合模式测试。"""

    def test_init(self) -> None:
        """测试初始化。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.stream_client is not None

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """测试启动适配器。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        adapter.stream_client.connect = AsyncMock()
        adapter.stream_client.start_receive_loop = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """测试停止适配器。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_called_once()

    def test_channel_type(self) -> None:
        """测试通道类型。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        assert adapter.channel_type == "dingtalk"
