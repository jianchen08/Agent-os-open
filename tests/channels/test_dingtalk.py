"""钉钉通道适配器测试。

测试 DingTalkInputAdapter、DingTalkOutputAdapter、DingTalkAdapter 组合
和 DingTalkStreamClient（Mock）的核心功能。
"""

from __future__ import annotations

import sys
import os
import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))

from channels.dingtalk.adapter import DingTalkAdapter, DingTalkInputAdapter, DingTalkOutputAdapter
from channels.dingtalk.stream_client import DingTalkStreamClient


# ═══════════════════════════════════════════════════════════
# DingTalkInputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestDingTalkInputAdapter:
    """DingTalkInputAdapter 输入适配器测试。"""

    def test_receive_text_message(self) -> None:
        """接收文本消息 → state。"""
        DingTalkInputAdapter()
        raw_msg = {
            "conversationId": "cid-1",
            "senderStaffId": "user_dt_1",
            "senderId": "sender-1",
            "msgtype": "text",
            "text": {"content": "Hello DingTalk"},
            "createAt": "1700000000000",
            "messageId": "msg-dt-1",
        }
        state = DingTalkInputAdapter._raw_to_state(raw_msg)
        assert state["user_input"] == "Hello DingTalk"
        assert state["_channel_type"] == "dingtalk"
        assert state["_channel_user_id"] == "user_dt_1"

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """enqueue + receive 流程。"""
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


# ═══════════════════════════════════════════════════════════
# DingTalkOutputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestDingTalkOutputAdapter:
    """DingTalkOutputAdapter 输出适配器测试。"""

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """发送正常结果。"""
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
        assert call_args[0][1] == "Hello result"

    @pytest.mark.asyncio
    async def test_send_error(self) -> None:
        """发送错误信息。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        state = {
            "raw_error": "Something went wrong",
            "_channel_user_id": "user_dt_1",
        }
        await adapter.send(state)
        client.send_message.assert_called_once()
        assert "Something went wrong" in client.send_message.call_args[0][1]

    @pytest.mark.asyncio
    async def test_send_no_user_id(self) -> None:
        """无 user_id 时跳过。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello",
            "_channel_user_id": "",
        }
        await adapter.send(state)
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulate(self) -> None:
        """流式累积文本。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("user_dt_1")

        chunk1 = {"text": "Hello ", "type": "token"}
        chunk2 = {"text": "World", "type": "token"}
        await adapter.send_stream(chunk1)
        await adapter.send_stream(chunk2)
        # 流式消息应累积但不发送
        assert adapter._accumulated_text == "Hello World"
        client.send_message.assert_not_called()


# ═══════════════════════════════════════════════════════════
# DingTalkAdapter 组合测试
# ═══════════════════════════════════════════════════════════


class TestDingTalkAdapter:
    """DingTalkAdapter 组合模式测试。"""

    def test_adapter_initialization(self) -> None:
        """验证组件初始化和回调绑定。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.stream_client is not None
        # 验证回调绑定（绑定方法用 == 而非 is）
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    def test_channel_type(self) -> None:
        """channel_type 属性为 'dingtalk'。"""
        adapter = DingTalkAdapter(client_id="test_id", client_secret="test_secret")
        assert adapter.channel_type == "dingtalk"


# ═══════════════════════════════════════════════════════════
# DingTalkStreamClient 测试（Mock）
# ═══════════════════════════════════════════════════════════


class TestDingTalkStreamClient:
    """DingTalkStreamClient 测试（Mock 外部调用）。"""

    def test_init(self) -> None:
        """测试初始化。"""
        client = DingTalkStreamClient(client_id="test_id", client_secret="test_secret")
        assert client._client_id == "test_id"
        assert client._client_secret == "test_secret"
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_send_message_format(self) -> None:
        """验证发送消息格式。"""
        client = DingTalkStreamClient(client_id="test_id", client_secret="test_secret")

        # Mock _ensure_token 避免 token 刷新请求
        client._ensure_token = AsyncMock()
        client._access_token = "test_token"

        # Mock response
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"code": "0", "message": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        await client.send_message("user_dt_1", "Hello")

        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        url = call_args[0][0]
        assert "robot/oToMessages/batchSend" in url
        body = call_args[1]["json"]
        assert body["robotCode"] == "test_id"
        assert body["userIds"] == ["user_dt_1"]
        assert body["msgKey"] == "text"
        assert body["msgParam"] == "Hello"
        headers = call_args[1]["headers"]
        assert headers["x-acs-dingtalk-access-token"] == "test_token"

    def test_compute_sign(self) -> None:
        """验证签名计算。"""
        client = DingTalkStreamClient(
            client_id="test_id", client_secret="test_secret"
        )
        timestamp = "1700000000000"
        sign = client._compute_sign(timestamp)

        # 手动计算期望签名
        import base64
        string_to_sign = f"{timestamp}\ntest_secret"
        expected_hmac = hmac.new(
            "test_secret".encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected_sign = base64.b64encode(expected_hmac).decode("utf-8")

        assert sign == expected_sign
        # 签名应为非空字符串
        assert isinstance(sign, str)
        assert len(sign) > 0
