# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""钉钉通道适配器测试。

测试 DingTalkInputAdapter、DingTalkOutputAdapter、DingTalkAdapter 组合
和 DingTalkStreamClient（Mock）的核心功能。
"""

from __future__ import annotations

import asyncio
import aiohttp
import hashlib
import hmac
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

from tests.channels.conftest import use_channel

use_channel("dingtalk")
from adapter import _extract_dingtalk_text, DingTalkAdapter, DingTalkInputAdapter, DingTalkOutputAdapter
from stream_client import DingTalkStreamClient

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

    @pytest.mark.asyncio
    async def test_send_stream_flush_sends_and_clears(self) -> None:
        """flush/end 标记 → 一次性发送累积文本并清空。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("user_dt_1")
        await adapter.send_stream({"text": "完整", "flush": True})
        client.send_message.assert_awaited_once_with("user_dt_1", "完整")
        assert adapter._accumulated_text == ""

    @pytest.mark.asyncio
    async def test_send_stream_flush_without_user_id_skips(self) -> None:
        """未设置 user_id 时 flush 不发送（无目标）。"""
        client = AsyncMock(spec=DingTalkStreamClient)
        adapter = DingTalkOutputAdapter(stream_client=client)
        await adapter.send_stream({"text": "x", "type": "end"})
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
            b"test_secret",
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        expected_sign = base64.b64encode(expected_hmac).decode("utf-8")

        assert sign == expected_sign
        # 签名应为非空字符串
        assert isinstance(sign, str)
        assert len(sign) > 0


# ═══════════════════════════════════════════════════════════
# DingTalkStreamClient 覆盖补齐（A5.2 批）
# ═══════════════════════════════════════════════════════════


class TestDingTalkStreamClientExtended:
    """token/端点/事件处理等未覆盖分支。"""

    def _client(self) -> DingTalkStreamClient:
        return DingTalkStreamClient(client_id="test_id", client_secret="test_secret")

    @pytest.mark.asyncio
    async def test_ensure_token_fresh_skips_request(self) -> None:
        client = self._client()
        client._access_token = "tok"
        client._token_expires = time.time() + 7000
        client._session = MagicMock()
        await client._ensure_token()
        client._session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_token_refreshes(self) -> None:
        client = self._client()
        client._access_token = "old"
        client._token_expires = time.time() - 10
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"accessToken": "new", "expireIn": 7200})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session
        await client._ensure_token()
        assert client._access_token == "new"
        url = mock_session.post.call_args[0][0]
        assert "oauth2/accessToken" in url
        body = mock_session.post.call_args[1]["json"]
        assert body["appKey"] == "test_id"

    @pytest.mark.asyncio
    async def test_ensure_token_no_session_raises(self) -> None:
        client = self._client()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client._ensure_token()

    @pytest.mark.asyncio
    async def test_get_endpoint_success_and_missing(self) -> None:
        client = self._client()
        client._ensure_token = AsyncMock()

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"endpoint": "wss://x"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session
        assert await client._get_endpoint() == "wss://x"
        # 请求带 clientId/timestamp/sign
        body = mock_session.post.call_args[1]["json"]
        assert body["clientId"] == "test_id" and body["sign"]

        mock_response2 = AsyncMock()
        mock_response2.json = AsyncMock(return_value={})
        mock_response2.__aenter__ = AsyncMock(return_value=mock_response2)
        mock_response2.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_response2)
        assert await client._get_endpoint() == ""

    @pytest.mark.asyncio
    async def test_send_message_error_code_returned(self) -> None:
        client = self._client()
        client._ensure_token = AsyncMock()
        client._access_token = "tok"
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"code": "400", "message": "bad"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        client._session = mock_session
        result = await client.send_message("u1", "hi")
        assert result["code"] == "400"  # 错误码透传，不抛

    @pytest.mark.asyncio
    async def test_handle_event_message_and_ack(self) -> None:
        client = self._client()
        client._ws = AsyncMock()
        client._ws.closed = False
        client._ws.send_json = AsyncMock()
        got = []

        async def cb(data):
            got.append(data)

        client.on_message = cb
        # 带 eventId → 先发 ACK 再回调 payload
        await client._handle_event(
            {
                "code": "1",
                "headers": {"eventType": "message", "eventId": "ev-1"},
                "data": {"msgtype": "text", "text": {"content": "hi"}},
            }
        )
        assert got and got[0]["msgtype"] == "text"
        client._ws.send_json.assert_awaited_once()
        ack = client._ws.send_json.call_args[0][0]
        assert ack["message"] == "OK" and ack["data"] == "ack"

    @pytest.mark.asyncio
    async def test_handle_event_non_message_and_no_callback(self) -> None:
        client = self._client()
        client._ws = AsyncMock()
        client._ws.closed = False
        await client._handle_event({"headers": {"eventType": "task_update"}})  # 非消息不回调
        client.on_message = None
        await client._handle_event({"headers": {"eventType": "message"}, "data": {}})  # 不抛

    @pytest.mark.asyncio
    async def test_disconnect_with_ws_and_task(self) -> None:
        client = self._client()
        ws = AsyncMock()
        ws.closed = False
        ws.close = AsyncMock()
        client._ws = ws
        task = asyncio.create_task(asyncio.sleep(10))
        client._receive_task = task
        session = MagicMock()
        session.closed = False
        session.close = AsyncMock()
        client._session = session
        await client.disconnect()
        ws.close.assert_awaited_once()
        session.close.assert_awaited_once()
        assert client._ws is None and client._session is None and client._receive_task is None

    def test_receive_loop_no_ws_returns(self) -> None:
        client = self._client()
        # _receive_loop 是 async，无 ws 直接返回
        asyncio.run(client._receive_loop())


class TestDingTalkAdapterExtended:
    """组合适配器 start/stop 与文本提取分支（A5.2 补）。"""

    def test_channel_type(self) -> None:
        adapter = DingTalkAdapter(client_id="cid", client_secret="sec")
        assert adapter.channel_type == "dingtalk"
        # stream_client 回调绑定 input_adapter
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    @pytest.mark.asyncio
    async def test_start_stop_delegate(self) -> None:
        adapter = DingTalkAdapter(client_id="cid", client_secret="sec")
        adapter.stream_client.connect = AsyncMock()
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_awaited_once()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_awaited_once()


class TestExtractDingtalkText:
    def test_variants(self) -> None:
        assert _extract_dingtalk_text("text", {"text": {"content": "hi"}}) == "hi"
        assert _extract_dingtalk_text("richText", {"richText": {"content": "rich"}}) == "rich"
        assert _extract_dingtalk_text("other", {"other": "x"}) == "x"
        assert _extract_dingtalk_text("other", {}) == ""


class TestDingTalkStreamLoop:
    """_receive_loop 消息迭代与连接重试（A5.2 补）。"""

    def _client(self) -> DingTalkStreamClient:
        return DingTalkStreamClient(client_id="test_id", client_secret="test_secret")

    @pytest.mark.asyncio
    async def test_receive_loop_text_and_error_messages(self) -> None:
        client = self._client()
        client._running = True
        client.on_message = AsyncMock()
        client._ws = _FakeWs(
            [
                _Msg(aiohttp.WSMsgType.TEXT, '{"headers": {"eventType": "message"}, "data": {}}'),
                _Msg(aiohttp.WSMsgType.TEXT, "{bad json"),  # 解析失败不抛
                _Msg(aiohttp.WSMsgType.CLOSED),
            ]
        )
        await client._receive_loop()
        client.on_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_receive_loop_ws_error_type_breaks(self) -> None:
        client = self._client()
        client._running = False
        client._ws = _FakeWs([_Msg(aiohttp.WSMsgType.ERROR)])
        await client._receive_loop()  # 不抛

    @pytest.mark.asyncio
    async def test_connect_retries_and_gives_up(self, monkeypatch) -> None:
        import stream_client as sc

        client = self._client()
        client._ensure_token = AsyncMock()
        fake_session = MagicMock()
        fake_session.ws_connect = AsyncMock(side_effect=OSError("conn refused"))
        # connect() 内部 aiohttp.ClientSession() 新建真实会话——monkeypatch 工厂
        monkeypatch.setattr(sc.aiohttp, "ClientSession", lambda: fake_session)
        client._get_endpoint = AsyncMock(return_value="wss://fake")
        client._max_retries = 2
        await client.connect()  # 重试耗尽后正常退出
        assert client._ws is None
        assert fake_session.ws_connect.call_count == 2


class _Msg:
    def __init__(self, type_, data=None):
        self.type = type_
        self.data = data


class _FakeWs:
    """async 迭代器形态的假 WS（async for 需要 __aiter__/__anext__）。"""

    def __init__(self, msgs):
        self._msgs = list(msgs)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._msgs:
            raise StopAsyncIteration
        return self._msgs.pop(0)


class TestDingTalkConnectPath:
    """connect 循环端点/连接/接收全路径（A5.2 补）。"""

    def _client(self) -> DingTalkStreamClient:
        return DingTalkStreamClient(client_id="test_id", client_secret="test_secret")

    @pytest.mark.asyncio
    async def test_connect_success_path(self, monkeypatch) -> None:
        import stream_client as sc

        client = self._client()
        client._ensure_token = AsyncMock()
        fake_session = MagicMock()
        fake_session.ws_connect = AsyncMock(return_value=_FakeWs([]))
        monkeypatch.setattr(sc.aiohttp, "ClientSession", lambda: fake_session)
        # 第一次循环端点成功；第二次端点空 → RuntimeError → 重试耗尽退出 while
        client._get_endpoint = AsyncMock(side_effect=["wss://fake", ""])
        client._receive_loop = AsyncMock()  # 直接返回，避免真实迭代
        client._max_retries = 1
        await client.connect()
        assert fake_session.ws_connect.call_count == 1
        client._receive_loop.assert_awaited_once()
    @pytest.mark.asyncio
    async def test_connect_endpoint_failure_retries(self, monkeypatch) -> None:
        import stream_client as sc

        client = self._client()
        client._ensure_token = AsyncMock()
        fake_session = MagicMock()
        fake_session.ws_connect = AsyncMock()
        monkeypatch.setattr(sc.aiohttp, "ClientSession", lambda: fake_session)
        client._get_endpoint = AsyncMock(return_value="")  # 端点为空 → RuntimeError
        client._max_retries = 1
        await client.connect()
        assert fake_session.ws_connect.call_count == 0  # 端点失败不尝试连接

    @pytest.mark.asyncio
    async def test_start_receive_loop_background_task(self) -> None:
        client = self._client()
        client.connect = AsyncMock()
        await client.start_receive_loop()
        assert client._receive_task is not None
        await client._receive_task
        client.connect.assert_awaited_once()
        client._receive_task = None

    @pytest.mark.asyncio
    async def test_get_endpoint_no_session_raises(self) -> None:
        client = self._client()
        client._ensure_token = AsyncMock()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client._get_endpoint()

    @pytest.mark.asyncio
    async def test_send_message_no_session_raises(self) -> None:
        client = self._client()
        client._ensure_token = AsyncMock()
        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message("u1", "hi")

    @pytest.mark.asyncio
    async def test_receive_loop_no_ws_returns_early(self) -> None:
        client = self._client()
        client._ws = None
        await client._receive_loop()  # 直接返回
