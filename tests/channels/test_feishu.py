"""飞书通道适配器测试。

测试 FeishuInputAdapter、FeishuOutputAdapter、FeishuAdapter 组合、
FeishuStreamClient（Mock）和 CardBuilder 的核心功能。
"""

from __future__ import annotations

import sys
import os
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))

from channels.feishu.adapter import FeishuAdapter, FeishuInputAdapter, FeishuOutputAdapter
from channels.feishu.stream_client import FeishuStreamClient
from channels.feishu.card_builder import CardBuilder


# ═══════════════════════════════════════════════════════════
# FeishuInputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestFeishuInputAdapter:
    """FeishuInputAdapter 输入适配器测试。"""

    def test_receive_text_message(self) -> None:
        """接收文本消息 → state。"""
        FeishuInputAdapter()
        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello feishu"}',
                    "create_time": "1700000000000",
                },
            },
        }
        # 直接测试 _raw_to_state 静态方法
        state = FeishuInputAdapter._raw_to_state(raw_msg)
        assert state["user_input"] == "hello feishu"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"

    @pytest.mark.asyncio
    async def test_receive_empty_queue(self) -> None:
        """空队列时 receive 阻塞（使用 asyncio.wait_for 超时验证）。"""
        adapter = FeishuInputAdapter()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.receive(), timeout=0.1)

    @pytest.mark.asyncio
    async def test_enqueue_and_receive(self) -> None:
        """enqueue_message 后 receive 能取出。"""
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


# ═══════════════════════════════════════════════════════════
# FeishuOutputAdapter 测试
# ═══════════════════════════════════════════════════════════


class TestFeishuOutputAdapter:
    """FeishuOutputAdapter 输出适配器测试。"""

    @pytest.mark.asyncio
    async def test_send_normal_result(self) -> None:
        """发送正常结果。"""
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
    async def test_send_error(self) -> None:
        """发送错误信息。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_error": "Something went wrong",
            "_channel_user_id": "ou_test",
        }
        await adapter.send(state)
        client.send_message.assert_called_once()
        assert "Something went wrong" in client.send_message.call_args[0][1]

    @pytest.mark.asyncio
    async def test_send_no_user_id(self) -> None:
        """无 user_id 时跳过发送。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        state = {
            "raw_result": "Hello",
            "_channel_user_id": "",
        }
        await adapter.send(state)
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_accumulate(self) -> None:
        """流式累积文本。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_test")

        chunk1 = {"text": "Hello ", "type": "token"}
        chunk2 = {"text": "World", "type": "token"}
        await adapter.send_stream(chunk1)
        await adapter.send_stream(chunk2)
        # 流式消息应累积但不发送
        assert adapter._accumulated_text == "Hello World"
        client.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_stream_flush(self) -> None:
        """flush 标记触发发送。"""
        client = AsyncMock(spec=FeishuStreamClient)
        adapter = FeishuOutputAdapter(stream_client=client)
        adapter.set_channel_user_id("ou_test")

        chunk = {"text": "Hello", "type": "token", "flush": True}
        await adapter.send_stream(chunk)
        client.send_message.assert_called_once()
        assert client.send_message.call_args[0][1] == "Hello"
        # 发送后累积文本清空
        assert adapter._accumulated_text == ""


# ═══════════════════════════════════════════════════════════
# FeishuAdapter 组合测试
# ═══════════════════════════════════════════════════════════


class TestFeishuAdapter:
    """FeishuAdapter 组合模式测试。"""

    def test_adapter_initialization(self) -> None:
        """验证组件初始化和回调绑定。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.input_adapter is not None
        assert adapter.output_adapter is not None
        assert adapter.stream_client is not None
        # 验证回调绑定（绑定方法用 == 而非 is）
        assert adapter.stream_client.on_message == adapter.input_adapter.enqueue_message

    def test_channel_type(self) -> None:
        """channel_type 属性为 'feishu'。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        assert adapter.channel_type == "feishu"

    @pytest.mark.asyncio
    async def test_start(self) -> None:
        """测试启动适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.connect = AsyncMock()
        await adapter.start()
        adapter.stream_client.connect.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop(self) -> None:
        """测试停止适配器。"""
        adapter = FeishuAdapter(app_id="test_id", app_secret="test_secret")
        adapter.stream_client.disconnect = AsyncMock()
        await adapter.stop()
        adapter.stream_client.disconnect.assert_called_once()


# ═══════════════════════════════════════════════════════════
# FeishuStreamClient 测试（Mock）
# ═══════════════════════════════════════════════════════════


class TestFeishuStreamClient:
    """FeishuStreamClient 测试（Mock 外部调用）。"""

    def test_init(self) -> None:
        """测试初始化。"""
        client = FeishuStreamClient(app_id="test_id", app_secret="test_secret")
        assert client._app_id == "test_id"
        assert client._app_secret == "test_secret"
        assert client.is_connected is False

    @pytest.mark.asyncio
    async def test_send_message_calls_api(self) -> None:
        """验证 send_message 的 API 调用格式。"""
        client = FeishuStreamClient(app_id="test_id", app_secret="test_secret")

        # Mock _ensure_token 避免 token 刷新请求
        client._ensure_token = AsyncMock()
        client._tenant_token = "test_token"

        # Mock session
        mock_response = AsyncMock()
        mock_response.json = AsyncMock(return_value={"code": 0, "msg": "ok"})
        mock_response.__aenter__ = AsyncMock(return_value=mock_response)
        mock_response.__aexit__ = AsyncMock(return_value=False)

        mock_session = MagicMock()
        mock_session.post = MagicMock(return_value=mock_response)
        mock_session.closed = False
        client._session = mock_session

        await client.send_message("ou_test", "Hello")

        # 验证调用格式
        mock_session.post.assert_called_once()
        call_args = mock_session.post.call_args
        url = call_args[0][0]
        assert "open-apis/im/v1/messages" in url
        body = call_args[1]["json"]
        assert body["receive_id"] == "ou_test"
        assert body["msg_type"] == "text"
        headers = call_args[1]["headers"]
        assert "Bearer test_token" in headers["Authorization"]

    @pytest.mark.asyncio
    async def test_connect_gets_endpoint(self) -> None:
        """验证 connect 流程中获取 endpoint。"""
        client = FeishuStreamClient(
            app_id="test_id", app_secret="test_secret", max_retries=1
        )

        # Mock _ensure_token
        client._ensure_token = AsyncMock()
        # Mock _get_endpoint 返回空字符串（触发重试，然后退出）
        client._get_endpoint = AsyncMock(return_value="")

        # connect 在 max_retries=1 时会因获取不到 endpoint 而退出
        await client.connect()
        client._ensure_token.assert_called()


# ═══════════════════════════════════════════════════════════
# CardBuilder 测试
# ═══════════════════════════════════════════════════════════


class TestCardBuilder:
    """CardBuilder 卡片构建器测试。"""

    def test_build_empty_card(self) -> None:
        """空卡片构建。"""
        card = CardBuilder().build()
        assert "elements" in card
        assert card["elements"] == []

    def test_build_text_card(self) -> None:
        """纯文本卡片。"""
        card = CardBuilder().add_markdown("Hello **world**").build()
        assert len(card["elements"]) == 1
        elem = card["elements"][0]
        assert elem["tag"] == "div"
        assert elem["text"]["content"] == "Hello **world**"
        assert elem["text"]["tag"] == "lark_md"

    def test_build_card_with_header(self) -> None:
        """带标题的卡片。"""
        card = CardBuilder().add_header("My Title").build()
        assert "header" in card
        assert card["header"]["title"]["content"] == "My Title"
        assert card["header"]["title"]["tag"] == "plain_text"

    def test_build_card_with_actions(self) -> None:
        """带按钮的卡片。"""
        actions = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": "Click"},
                "value": {"action": "click"},
            }
        ]
        card = CardBuilder().add_action(actions).build()
        assert len(card["elements"]) == 1
        elem = card["elements"][0]
        assert elem["tag"] == "action"
        assert len(elem["actions"]) == 1

    def test_build_text_card_template(self) -> None:
        """预置模板：纯文本卡片。"""
        card = CardBuilder.build_text_card(title="Greeting", content="Hi there")
        assert card["header"]["title"]["content"] == "Greeting"
        assert len(card["elements"]) == 1
        assert card["elements"][0]["text"]["content"] == "Hi there"

    def test_build_action_card_template(self) -> None:
        """预置模板：带按钮的卡片。"""
        buttons = [
            {"text": "OK", "value": {"action": "ok"}},
            {"text": "Cancel", "value": {"action": "cancel"}},
        ]
        card = CardBuilder.build_action_card(
            title="Confirm", content="Are you sure?", buttons=buttons
        )
        assert card["header"]["title"]["content"] == "Confirm"
        assert card["elements"][0]["text"]["content"] == "Are you sure?"
        action_elem = card["elements"][1]
        assert action_elem["tag"] == "action"
        assert len(action_elem["actions"]) == 2

    def test_add_hr_and_note(self) -> None:
        """分割线和备注。"""
        card = (
            CardBuilder()
            .add_markdown("Content")
            .add_hr()
            .add_note("This is a note")
            .build()
        )
        assert len(card["elements"]) == 3
        assert card["elements"][1]["tag"] == "hr"
        assert card["elements"][2]["tag"] == "note"
        assert card["elements"][2]["elements"][0]["content"] == "This is a note"

    def test_build_returns_serializable_dict(self) -> None:
        """build 返回可序列化的 dict。"""
        card = CardBuilder().add_markdown("test").build()
        serialized = json.dumps(card)
        assert isinstance(serialized, str)
