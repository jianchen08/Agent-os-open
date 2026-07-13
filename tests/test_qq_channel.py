"""QQ 通道适配器单元测试。

覆盖范围：
- OneBotClient: WebSocket 服务端接收事件、HTTP API 发送消息、自动重连、on_message 回调
- QQInputAdapter: 消息队列、raw → state 转换
- QQOutputAdapter: 发送正常结果、错误消息、流式累积
- QQAdapter: 组合模式、channel_type、生命周期管理
- MessageNormalizer: QQ 标准化/反标准化注册
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pipeline.types import StateKeys


# ═══════════════════════════════════════════════════════════════
# OneBotClient 测试
# ═══════════════════════════════════════════════════════════════


class TestOneBotClient:
    """OneBotClient 单元测试。"""

    def test_init_default_params(self) -> None:
        """默认参数初始化。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        assert client._ws_host == "0.0.0.0"
        assert client._ws_port == 8080
        assert client._http_api_url == "http://127.0.0.1:5700"
        assert client._max_retries == 5
        assert client._base_delay == 1.0
        assert client.on_message is None

    def test_init_custom_params(self) -> None:
        """自定义参数初始化。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient(
            ws_host="192.168.1.1",
            ws_port=9090,
            http_api_url="http://onebot:5700",
            max_retries=3,
            base_delay=2.0,
        )
        assert client._ws_host == "192.168.1.1"
        assert client._ws_port == 9090
        assert client._http_api_url == "http://onebot:5700"
        assert client._max_retries == 3
        assert client._base_delay == 2.0

    def test_is_connected_false_initially(self) -> None:
        """初始状态未连接。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_send_message_text(self) -> None:
        """通过 HTTP API 发送文本消息。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient(http_api_url="http://onebot:5700")
        client._session = AsyncMock()

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok", "data": {"message_id": 123}})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        client._session.post = MagicMock(return_value=mock_response)

        result = await client.send_message(
            user_id=123456789,
            content="你好",
            message_type="private",
        )

        assert result["status"] == "ok"
        client._session.post.assert_called_once()
        call_args = client._session.post.call_args
        assert call_args[0][0] == "http://onebot:5700/send_msg"
        body = call_args[1]["json"]
        assert body["message_type"] == "private"
        assert body["user_id"] == 123456789

    @pytest.mark.asyncio
    async def test_send_message_group(self) -> None:
        """发送群消息。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient(http_api_url="http://onebot:5700")
        client._session = AsyncMock()

        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"status": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)
        client._session.post = MagicMock(return_value=mock_response)

        await client.send_message(
            user_id=98765,
            content="群消息",
            message_type="group",
        )

        body = client._session.post.call_args[1]["json"]
        assert body["message_type"] == "group"
        assert body["group_id"] == 98765

    @pytest.mark.asyncio
    async def test_send_message_no_session_raises(self) -> None:
        """未初始化 session 时发送消息抛出异常。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        client._session = None

        with pytest.raises(RuntimeError, match="Session not initialized"):
            await client.send_message(user_id=123, content="test")

    @pytest.mark.asyncio
    async def test_handle_event_private_message(self) -> None:
        """处理 OneBot v11 私聊消息事件。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        received_messages: list[dict[str, Any]] = []
        client.on_message = AsyncMock(side_effect=lambda m: received_messages.append(m))

        event = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 12345,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
            },
            "time": 1234567890,
            "self_id": 987654321,
        }

        await client._handle_event(event)

        client.on_message.assert_called_once()
        assert received_messages[0]["post_type"] == "message"
        assert received_messages[0]["message_type"] == "private"

    @pytest.mark.asyncio
    async def test_handle_event_group_message(self) -> None:
        """处理 OneBot v11 群聊消息事件。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        client.on_message = AsyncMock()

        event = {
            "post_type": "message",
            "message_type": "group",
            "sub_type": "normal",
            "message_id": 54321,
            "group_id": 11111,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "群消息"}}],
            "raw_message": "群消息",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
            },
            "time": 1234567890,
            "self_id": 987654321,
        }

        await client._handle_event(event)
        client.on_message.assert_called_once()
        msg = client.on_message.call_args[0][0]
        assert msg["message_type"] == "group"
        assert msg["group_id"] == 11111

    @pytest.mark.asyncio
    async def test_handle_event_non_message_ignored(self) -> None:
        """非消息事件不触发 on_message 回调。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        client.on_message = AsyncMock()

        event = {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "time": 1234567890,
            "self_id": 987654321,
        }

        await client._handle_event(event)
        client.on_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_event_no_callback(self) -> None:
        """没有 on_message 回调时不报错。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()

        event = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 12345,
            "user_id": 123456789,
            "message": "test",
            "time": 1234567890,
            "self_id": 987654321,
        }

        # 不应抛出异常
        await client._handle_event(event)

    @pytest.mark.asyncio
    async def test_disconnect(self) -> None:
        """断开连接清理资源。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        client._running = True
        mock_server = AsyncMock()
        client._ws_server = mock_server
        client._receive_task = None
        mock_session = AsyncMock()
        mock_session.closed = False
        mock_session.close = AsyncMock()
        client._session = mock_session

        await client.disconnect()

        assert client._running is False
        mock_server.cleanup.assert_called_once()
        mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_text_message(self) -> None:
        """构建文本消息段。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        msg = client._build_message("你好世界")
        assert msg == [{"type": "text", "data": {"text": "你好世界"}}]

    @pytest.mark.asyncio
    async def test_build_array_message(self) -> None:
        """构建 Array 格式消息段。"""
        from channels.qq.onebot_client import OneBotClient

        client = OneBotClient()
        segments = [
            {"type": "text", "data": {"text": "你好"}},
            {"type": "image", "data": {"file": "test.jpg"}},
        ]
        msg = client._build_message(segments)
        assert msg == segments


# ═══════════════════════════════════════════════════════════════
# QQInputAdapter 测试
# ═══════════════════════════════════════════════════════════════


class TestQQInputAdapter:
    """QQInputAdapter 单元测试。"""

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """消息入队和出队转换。"""
        from channels.qq.adapter import QQInputAdapter

        adapter = QQInputAdapter()

        raw_message = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 12345,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
            },
            "time": 1234567890,
            "self_id": 987654321,
        }

        await adapter.enqueue_message(raw_message)
        state = await adapter.receive()

        assert state["user_input"] == "你好"
        assert state[StateKeys.CORE_TYPE] == "llm_call"
        assert state[StateKeys.SHOULD_STOP] is False
        assert state["iteration"] == 1
        assert state["_channel_type"] == "qq"
        assert state["_channel_user_id"] == "123456789"
        assert state["_raw_message"] == raw_message

    @pytest.mark.asyncio
    async def test_raw_to_state_with_cq_code(self) -> None:
        """CQ 码格式的消息解析。"""
        from channels.qq.adapter import QQInputAdapter

        adapter = QQInputAdapter()

        raw_message = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 11111,
            "message_id": 54321,
            "user_id": 123456789,
            "message": "你好[CQ:at,qq=all]",
            "raw_message": "你好[CQ:at,qq=all]",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
            },
            "time": 1234567890,
            "self_id": 987654321,
        }

        await adapter.enqueue_message(raw_message)
        state = await adapter.receive()

        # CQ 码应该被提取纯文本部分
        assert "你好" in state["user_input"]
        assert state["_channel_type"] == "qq"

    @pytest.mark.asyncio
    async def test_raw_to_state_group_message(self) -> None:
        """群聊消息转换包含 group_id。"""
        from channels.qq.adapter import QQInputAdapter

        adapter = QQInputAdapter()

        raw_message = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 11111,
            "message_id": 54321,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "群消息"}}],
            "raw_message": "群消息",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
            },
            "time": 1234567890,
            "self_id": 987654321,
        }

        await adapter.enqueue_message(raw_message)
        state = await adapter.receive()

        assert state["user_input"] == "群消息"
        assert state["_group_id"] == 11111
        assert state["_message_type"] == "group"


# ═══════════════════════════════════════════════════════════════
# QQOutputAdapter 测试
# ═══════════════════════════════════════════════════════════════


class TestQQOutputAdapter:
    """QQOutputAdapter 单元测试。"""

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """发送正常结果。"""
        from channels.qq.adapter import QQOutputAdapter
        from channels.qq.onebot_client import OneBotClient

        mock_client = AsyncMock(spec=OneBotClient)
        adapter = QQOutputAdapter(mock_client)
        adapter.set_channel_user_id("123456789")

        state = {
            StateKeys.RAW_RESULT: "回复内容",
            "_channel_user_id": "123456789",
            "_message_type": "private",
        }

        await adapter.send(state)
        mock_client.send_message.assert_called_once_with(
            user_id=123456789,
            content="回复内容",
            message_type="private",
        )

    @pytest.mark.asyncio
    async def test_send_error_result(self) -> None:
        """发送错误消息。"""
        from channels.qq.adapter import QQOutputAdapter
        from channels.qq.onebot_client import OneBotClient

        mock_client = AsyncMock(spec=OneBotClient)
        adapter = QQOutputAdapter(mock_client)
        adapter.set_channel_user_id("123456789")

        state = {
            StateKeys.RAW_ERROR: "出错了",
            "_channel_user_id": "123456789",
            "_message_type": "private",
        }

        await adapter.send(state)
        mock_client.send_message.assert_called_once()
        call_args = mock_client.send_message.call_args
        assert "错误" in call_args[1]["content"] or "出错了" in call_args[1]["content"]

    @pytest.mark.asyncio
    async def test_send_no_user_id_skips(self) -> None:
        """无 user_id 时跳过发送。"""
        from channels.qq.adapter import QQOutputAdapter
        from channels.qq.onebot_client import OneBotClient

        mock_client = AsyncMock(spec=OneBotClient)
        adapter = QQOutputAdapter(mock_client)

        state = {StateKeys.RAW_RESULT: "内容"}

        await adapter.send(state)
        mock_client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulates(self) -> None:
        """流式输出累积文本。"""
        from channels.qq.adapter import QQOutputAdapter
        from channels.qq.onebot_client import OneBotClient

        mock_client = AsyncMock(spec=OneBotClient)
        adapter = QQOutputAdapter(mock_client)
        adapter.set_channel_user_id("123456789")

        # 累积多个 chunk
        await adapter.send_stream({"text": "你", "type": "token"})
        await adapter.send_stream({"text": "好", "type": "token"})
        assert mock_client.send_message.call_count == 0  # 还未 flush

        # flush 触发发送
        await adapter.send_stream({"text": "！", "flush": True})
        mock_client.send_message.assert_called_once_with(
            user_id=123456789,
            content="你好！",
            message_type="private",
        )

    @pytest.mark.asyncio
    async def test_send_stream_end_flushes(self) -> None:
        """流结束时发送累积内容。"""
        from channels.qq.adapter import QQOutputAdapter
        from channels.qq.onebot_client import OneBotClient

        mock_client = AsyncMock(spec=OneBotClient)
        adapter = QQOutputAdapter(mock_client)
        adapter.set_channel_user_id("123456789")

        await adapter.send_stream({"text": "完成", "type": "end"})
        mock_client.send_message.assert_called_once_with(
            user_id=123456789,
            content="完成",
            message_type="private",
        )


# ═══════════════════════════════════════════════════════════════
# QQAdapter 测试
# ═══════════════════════════════════════════════════════════════


class TestQQAdapter:
    """QQAdapter 组合模式测试。"""

    def test_channel_type(self) -> None:
        """channel_type 返回 'qq'。"""
        from channels.qq.adapter import QQAdapter

        adapter = QQAdapter()
        assert adapter.channel_type == "qq"

    def test_composition_pattern(self) -> None:
        """组合模式：包含 input_adapter 和 output_adapter。"""
        from channels.qq.adapter import QQAdapter, QQInputAdapter, QQOutputAdapter

        adapter = QQAdapter()
        assert isinstance(adapter.input_adapter, QQInputAdapter)
        assert isinstance(adapter.output_adapter, QQOutputAdapter)

    def test_stream_client_created(self) -> None:
        """创建并持有 OneBotClient。"""
        from channels.qq.adapter import QQAdapter
        from channels.qq.onebot_client import OneBotClient

        adapter = QQAdapter(
            ws_host="0.0.0.0",
            ws_port=8080,
            http_api_url="http://localhost:5700",
        )
        assert isinstance(adapter.stream_client, OneBotClient)

    def test_on_message_bound(self) -> None:
        """stream_client 的 on_message 绑定到 input_adapter.enqueue_message。"""
        from channels.qq.adapter import QQAdapter

        adapter = QQAdapter()
        assert adapter.stream_client.on_message is not None
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """启动适配器调用 stream_client.connect。"""
        from channels.qq.adapter import QQAdapter

        adapter = QQAdapter()
        adapter.stream_client.connect = AsyncMock()

        await adapter.start()
        adapter.stream_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """停止适配器调用 stream_client.disconnect。"""
        from channels.qq.adapter import QQAdapter

        adapter = QQAdapter()
        adapter.stream_client.disconnect = AsyncMock()

        await adapter.stop()
        adapter.stream_client.disconnect.assert_called_once()


# ═══════════════════════════════════════════════════════════════
# MessageNormalizer QQ 注册测试
# ═══════════════════════════════════════════════════════════════


class TestMessageNormalizerQQ:
    """MessageNormalizer 中 QQ 标准化器测试。"""

    def test_qq_normalizer_registered(self) -> None:
        """QQ 标准化器已注册。"""
        from channels.gateway.message_normalizer import MessageNormalizer

        normalizer = MessageNormalizer()
        msg = normalizer.normalize("qq", self._sample_private_message())
        assert msg.channel_type == "qq"

    def test_normalize_private_message(self) -> None:
        """标准化私聊消息。"""
        from channels.gateway.message_normalizer import MessageNormalizer

        normalizer = MessageNormalizer()
        raw = self._sample_private_message()
        msg = normalizer.normalize("qq", raw)

        assert msg.message_id == "12345"
        assert msg.channel_type == "qq"
        assert msg.channel_user_id == "123456789"
        assert msg.unified_user_id == "qq:123456789"
        assert msg.content == "你好"
        assert msg.content_type == "text"

    def test_normalize_group_message(self) -> None:
        """标准化群聊消息。"""
        from channels.gateway.message_normalizer import MessageNormalizer

        normalizer = MessageNormalizer()
        raw = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 11111,
            "message_id": 54321,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "群消息"}}],
            "raw_message": "群消息",
            "sender": {"user_id": 123456789, "nickname": "测试用户"},
            "time": 1234567890,
            "self_id": 987654321,
        }
        msg = normalizer.normalize("qq", raw)

        assert msg.message_id == "54321"
        assert msg.channel_type == "qq"
        assert msg.content == "群消息"
        assert msg.metadata.get("group_id") == 11111
        assert msg.metadata.get("message_type") == "group"

    def test_normalize_string_message(self) -> None:
        """标准化字符串格式消息（非 Array）。"""
        from channels.gateway.message_normalizer import MessageNormalizer

        normalizer = MessageNormalizer()
        raw = {
            "post_type": "message",
            "message_type": "private",
            "message_id": 999,
            "user_id": 111,
            "message": "纯文本消息",
            "raw_message": "纯文本消息",
            "sender": {"user_id": 111, "nickname": "用户"},
            "time": 1234567890,
            "self_id": 987654321,
        }
        msg = normalizer.normalize("qq", raw)
        assert msg.content == "纯文本消息"

    def test_denormalize_text(self) -> None:
        """反标准化文本响应。"""
        from channels.gateway.message_normalizer import MessageNormalizer
        from channels.gateway.unified_types import UnifiedResponse

        normalizer = MessageNormalizer()
        response = UnifiedResponse(
            message_id="12345",
            channel_type="qq",
            content="回复内容",
            content_type="text",
        )
        result = normalizer.denormalize("qq", response)

        assert result["message_type"] == "private"
        assert "message" in result
        assert result["message"][0]["type"] == "text"
        assert result["message"][0]["data"]["text"] == "回复内容"

    def test_denormalize_unsupported_channel(self) -> None:
        """不支持的渠道类型抛出 ValueError。"""
        from channels.gateway.message_normalizer import MessageNormalizer
        from channels.gateway.unified_types import UnifiedResponse

        normalizer = MessageNormalizer()
        response = UnifiedResponse(
            message_id="1",
            channel_type="unknown",
            content="test",
            content_type="text",
        )
        with pytest.raises(ValueError, match="Unsupported channel type"):
            normalizer.denormalize("unknown_channel", response)

    def _sample_private_message(self) -> dict[str, Any]:
        """返回一个标准的 OneBot v11 私聊消息样本。"""
        return {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "message_id": 12345,
            "user_id": 123456789,
            "message": [{"type": "text", "data": {"text": "你好"}}],
            "raw_message": "你好",
            "sender": {
                "user_id": 123456789,
                "nickname": "测试用户",
                "sex": "unknown",
                "age": 0,
            },
            "time": 1234567890,
            "self_id": 987654321,
        }
