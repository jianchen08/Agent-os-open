"""消息标准化器测试。"""

from __future__ import annotations


import pytest

from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse


class TestMessageNormalizer:
    """MessageNormalizer 测试。"""

    def setup_method(self) -> None:
        """每个测试方法前创建 normalizer 实例。"""
        self.normalizer = MessageNormalizer()

    # ── 飞书消息标准化 ──────────────────────────────

    def test_normalize_feishu_text_message(self) -> None:
        """测试飞书文本消息标准化。"""
        raw = {
            "header": {
                "event_id": "evt-001",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {
                        "open_id": "ou_abc123",
                        "user_id": "uid_abc123",
                    },
                },
                "message": {
                    "message_id": "msg-feishu-001",
                    "message_type": "text",
                    "content": '{"text":"Hello from Feishu"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "feishu"
        assert result.channel_user_id == "ou_abc123"
        assert result.unified_user_id == "feishu:ou_abc123"
        assert result.content == "Hello from Feishu"
        assert result.content_type == "text"
        assert result.raw_message is raw

    def test_normalize_feishu_image_message(self) -> None:
        """测试飞书图片消息标准化。"""
        raw = {
            "header": {
                "event_id": "evt-002",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_xyz"},
                },
                "message": {
                    "message_id": "msg-feishu-002",
                    "message_type": "image",
                    "content": '{"image_key":"img_xxx"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content_type == "image"

    def test_normalize_feishu_file_message(self) -> None:
        """测试飞书文件消息标准化。"""
        raw = {
            "header": {
                "event_id": "evt-003",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_file"},
                },
                "message": {
                    "message_id": "msg-feishu-003",
                    "message_type": "file",
                    "content": '{"file_key":"file_xxx"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content_type == "file"

    def test_normalize_feishu_unsupported_type_fallback_to_text(self) -> None:
        """不支持的消息类型降级为纯文本。"""
        raw = {
            "header": {
                "event_id": "evt-004",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_unsupported"},
                },
                "message": {
                    "message_id": "msg-feishu-004",
                    "message_type": "interactive",
                    "content": '{"text":"card interaction"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content_type == "text"

    # ── 钉钉消息标准化 ──────────────────────────────

    def test_normalize_dingtalk_text_message(self) -> None:
        """测试钉钉文本消息标准化。"""
        raw = {
            "conversationId": "cid-001",
            "senderStaffId": "user_dt_001",
            "senderId": "sender-id-001",
            "msgtype": "text",
            "text": {"content": "Hello from DingTalk"},
            "createAt": "1700000000000",
            "messageId": "msg-dt-001",
        }
        result = self.normalizer.normalize("dingtalk", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "dingtalk"
        assert result.channel_user_id == "user_dt_001"
        assert result.unified_user_id == "dingtalk:user_dt_001"
        assert result.content == "Hello from DingTalk"
        assert result.content_type == "text"

    def test_normalize_dingtalk_rich_text_message(self) -> None:
        """测试钉钉富文本消息降级。"""
        raw = {
            "conversationId": "cid-002",
            "senderStaffId": "user_dt_002",
            "msgtype": "richText",
            "richText": {"content": "Rich text content"},
            "createAt": "1700000000000",
            "messageId": "msg-dt-002",
        }
        result = self.normalizer.normalize("dingtalk", raw)
        assert result.content_type == "text"

    # ── 不支持的渠道 ────────────────────────────────

    def test_normalize_unsupported_channel_raises(self) -> None:
        """不支持的渠道应抛出 ValueError。"""
        with pytest.raises(ValueError, match="Unsupported channel"):
            self.normalizer.normalize("slack", {"data": "irrelevant"})

    # ── 反标准化（响应 → 渠道格式）─────────────────

    def test_denormalize_feishu_text_response(self) -> None:
        """测试飞书文本响应反标准化。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="Hi from bot",
            content_type="text",
            card_config=None,
            metadata={},
        )
        result = self.normalizer.denormalize("feishu", resp)
        assert result["msg_type"] == "text"
        assert result["content"]["text"] == "Hi from bot"

    def test_denormalize_feishu_card_response(self) -> None:
        """测试飞书卡片响应反标准化。"""
        card = {"header": {"title": "Card"}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="",
            content_type="card",
            card_config=card,
            metadata={},
        )
        result = self.normalizer.denormalize("feishu", resp)
        assert result["msg_type"] == "interactive"
        assert result["content"]["card"] is card

    def test_denormalize_dingtalk_text_response(self) -> None:
        """测试钉钉文本响应反标准化。"""
        resp = UnifiedResponse(
            message_id="msg-002",
            channel_type="dingtalk",
            content="Hi from dingtalk bot",
            content_type="text",
            card_config=None,
            metadata={},
        )
        result = self.normalizer.denormalize("dingtalk", resp)
        assert result["msgtype"] == "text"
        assert result["text"]["content"] == "Hi from dingtalk bot"

    def test_denormalize_dingtalk_card_response_fallback(self) -> None:
        """钉钉不支持卡片时降级为 markdown。"""
        card = {"header": {"title": "Card"}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-002",
            channel_type="dingtalk",
            content="Card content",
            content_type="card",
            card_config=card,
            metadata={},
        )
        result = self.normalizer.denormalize("dingtalk", resp)
        # 钉钉卡片降级为 markdown
        assert result["msgtype"] == "markdown"
        assert "Card content" in result["markdown"]["text"]

    def test_denormalize_unsupported_channel_raises(self) -> None:
        """不支持的渠道反标准化应抛出 ValueError。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="slack",
            content="test",
            content_type="text",
            card_config=None,
            metadata={},
        )
        with pytest.raises(ValueError, match="Unsupported channel"):
            self.normalizer.denormalize("slack", resp)

    # ── 边界场景 ────────────────────────────────────

    def test_normalize_feishu_missing_sender(self) -> None:
        """飞书消息缺少 sender 字段应优雅降级。"""
        raw = {
            "header": {
                "event_id": "evt-bad",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "message": {
                    "message_id": "msg-bad",
                    "message_type": "text",
                    "content": '{"text":"orphan message"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.channel_user_id == ""
        assert result.unified_user_id == "feishu:unknown"

    def test_normalize_feishu_invalid_content_json(self) -> None:
        """飞书消息 content JSON 解析失败应优雅降级。"""
        raw = {
            "header": {
                "event_id": "evt-bad-json",
                "event_type": "im.message.receive_v1",
            },
            "event": {
                "sender": {
                    "sender_id": {"open_id": "ou_bad_json"},
                },
                "message": {
                    "message_id": "msg-bad-json",
                    "message_type": "text",
                    "content": "not valid json",
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content == "not valid json"

    def test_normalize_dingtalk_missing_fields(self) -> None:
        """钉钉消息缺少字段应优雅降级。"""
        raw = {
            "msgtype": "text",
            "text": {"content": "partial message"},
        }
        result = self.normalizer.normalize("dingtalk", raw)
        assert result.content == "partial message"
        assert result.channel_user_id == ""
