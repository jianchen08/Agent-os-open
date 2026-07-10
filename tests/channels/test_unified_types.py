"""统一消息类型测试。"""

from __future__ import annotations

import time

from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse


class TestUnifiedMessage:
    """UnifiedMessage 数据类测试。"""

    def test_create_with_defaults(self) -> None:
        """测试使用默认值创建 UnifiedMessage。"""
        msg = UnifiedMessage(
            message_id="msg-001",
            channel_type="feishu",
            channel_user_id="ou_xxx",
            unified_user_id="user_xxx",
            content="Hello",
            content_type="text",
            raw_message={"event": {"message": {"content": "Hello"}}},
            timestamp=time.time(),
            metadata={},
        )
        assert msg.message_id == "msg-001"
        assert msg.channel_type == "feishu"
        assert msg.channel_user_id == "ou_xxx"
        assert msg.unified_user_id == "user_xxx"
        assert msg.content == "Hello"
        assert msg.content_type == "text"
        assert isinstance(msg.raw_message, dict)
        assert isinstance(msg.metadata, dict)

    def test_different_channel_types(self) -> None:
        """测试不同渠道类型。"""
        for channel_type in ("feishu", "dingtalk", "websocket", "cli"):
            msg = UnifiedMessage(
                message_id="msg-001",
                channel_type=channel_type,
                channel_user_id="user-1",
                unified_user_id="user-1",
                content="test",
                content_type="text",
                raw_message={},
                timestamp=0.0,
                metadata={},
            )
            assert msg.channel_type == channel_type

    def test_content_types(self) -> None:
        """测试不同内容类型。"""
        for content_type in ("text", "card", "image", "file"):
            msg = UnifiedMessage(
                message_id="msg-001",
                channel_type="feishu",
                channel_user_id="user-1",
                unified_user_id="user-1",
                content="test",
                content_type=content_type,
                raw_message={},
                timestamp=0.0,
                metadata={},
            )
            assert msg.content_type == content_type


class TestUnifiedResponse:
    """UnifiedResponse 数据类测试。"""

    def test_create_text_response(self) -> None:
        """测试创建文本响应。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="Hi there",
            content_type="text",
            card_config=None,
            metadata={},
        )
        assert resp.message_id == "msg-001"
        assert resp.content == "Hi there"
        assert resp.content_type == "text"
        assert resp.card_config is None

    def test_create_card_response(self) -> None:
        """测试创建卡片响应。"""
        card = {"header": {"title": "Test"}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="",
            content_type="card",
            card_config=card,
            metadata={"template": "text_card"},
        )
        assert resp.content_type == "card"
        assert resp.card_config is not None
        assert resp.card_config["header"]["title"] == "Test"
