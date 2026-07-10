"""消息标准化器补充测试。

覆盖现有测试未覆盖的渠道和边界场景：
- 企业微信（wecom）消息标准化与反标准化
- QQ 消息标准化与反标准化
- 自定义渠道动态注册
- 时间戳解析边界
- 卡片标题提取
"""

from __future__ import annotations

import json
import time

import pytest

from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse


class TestWecomNormalization:
    """企业微信消息标准化测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_normalize_wecom_text_message(self) -> None:
        """企业微信文本消息 → UnifiedMessage。"""
        raw = {
            "FromUserName": "user_wecom_001",
            "ToUserName": "agent_001",
            "MsgType": "text",
            "Content": "Hello WeCom",
            "MsgId": "msg-wecom-001",
            "CreateTime": "1700000000",
            "AgentID": "1000001",
        }
        result = self.normalizer.normalize("wecom", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "wecom"
        assert result.channel_user_id == "user_wecom_001"
        assert result.unified_user_id == "wecom:user_wecom_001"
        assert result.content == "Hello WeCom"
        assert result.content_type == "text"

    def test_normalize_wecom_image_message(self) -> None:
        """企业微信图片消息。"""
        raw = {
            "FromUserName": "user_img",
            "MsgType": "image",
            "PicUrl": "http://example.com/pic.jpg",
            "MsgId": "msg-img-001",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content_type == "image"
        assert result.content == "http://example.com/pic.jpg"

    def test_normalize_wecom_voice_message_with_recognition(self) -> None:
        """企业微信语音消息（含语音识别结果）。"""
        raw = {
            "FromUserName": "user_voice",
            "MsgType": "voice",
            "Recognition": "你好世界",
            "MsgId": "msg-voice-001",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "你好世界"
        assert result.content_type == "text"

    def test_normalize_wecom_voice_message_without_recognition(self) -> None:
        """企业微信语音消息（无识别结果）→ [语音]。"""
        raw = {
            "FromUserName": "user_voice",
            "MsgType": "voice",
            "MsgId": "msg-voice-002",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "[语音]"

    def test_normalize_wecom_video_message(self) -> None:
        """企业微信视频消息 → [视频]。"""
        raw = {
            "FromUserName": "user_video",
            "MsgType": "video",
            "MsgId": "msg-video-001",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "[视频]"

    def test_normalize_wecom_location_message(self) -> None:
        """企业微信位置消息。"""
        raw = {
            "FromUserName": "user_loc",
            "MsgType": "location",
            "Label": "北京市海淀区",
            "MsgId": "msg-loc-001",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert "[位置]" in result.content
        assert "北京市海淀区" in result.content

    def test_normalize_wecom_location_message_no_label(self) -> None:
        """企业微信位置消息（无标签）。"""
        raw = {
            "FromUserName": "user_loc",
            "MsgType": "location",
            "MsgId": "msg-loc-002",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "[位置]"

    def test_normalize_wecom_link_message(self) -> None:
        """企业微信链接消息。"""
        raw = {
            "FromUserName": "user_link",
            "MsgType": "link",
            "Description": "这是一篇有趣的文章",
            "MsgId": "msg-link-001",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.content == "这是一篇有趣的文章"

    def test_normalize_wecom_missing_sender(self) -> None:
        """企业微信消息缺少发送者 → 降级为 unknown。"""
        raw = {
            "MsgType": "text",
            "Content": "orphan",
            "MsgId": "msg-orphan",
            "CreateTime": "1700000000",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.channel_user_id == ""
        assert result.unified_user_id == "wecom:unknown"

    def test_normalize_wecom_metadata_includes_agent_and_to(self) -> None:
        """企业微信消息 metadata 包含 AgentID 和 ToUserName。"""
        raw = {
            "FromUserName": "user_001",
            "ToUserName": "corp_001",
            "MsgType": "text",
            "Content": "test",
            "MsgId": "msg-meta",
            "CreateTime": "1700000000",
            "AgentID": "1000002",
        }
        result = self.normalizer.normalize("wecom", raw)
        assert result.metadata["agent_id"] == "1000002"
        assert result.metadata["to_user"] == "corp_001"

    def test_normalize_wecom_invalid_timestamp(self) -> None:
        """企业微信无效时间戳 → 使用当前时间。"""
        raw = {
            "FromUserName": "user_ts",
            "MsgType": "text",
            "Content": "test",
            "MsgId": "msg-ts",
            "CreateTime": "not_a_number",
        }
        before = time.time()
        result = self.normalizer.normalize("wecom", raw)
        after = time.time()
        assert before <= result.timestamp <= after

    # ── 企业微信反标准化 ──

    def test_denormalize_wecom_text_response(self) -> None:
        """UnifiedResponse(text) → 企业微信文本格式。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="wecom",
            content="Hi from bot",
            content_type="text",
        )
        result = self.normalizer.denormalize("wecom", resp)
        assert result["msgtype"] == "text"
        assert result["text"]["content"] == "Hi from bot"

    def test_denormalize_wecom_card_fallback_to_markdown(self) -> None:
        """企业微信不支持卡片 → 降级为 markdown。"""
        card = {"header": {"title": "Card Title"}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="wecom",
            content="Card content",
            content_type="card",
            card_config=card,
        )
        result = self.normalizer.denormalize("wecom", resp)
        assert result["msgtype"] == "markdown"
        assert "Card Title" in result["markdown"]["content"]
        assert "Card content" in result["markdown"]["content"]

    def test_denormalize_wecom_card_title_string_type(self) -> None:
        """企业微信卡片标题为字符串类型时的降级。"""
        card = {"header": {"title": "String Title"}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="wecom",
            content="Content",
            content_type="card",
            card_config=card,
        )
        result = self.normalizer.denormalize("wecom", resp)
        assert result["msgtype"] == "markdown"
        assert "String Title" in result["markdown"]["content"]


class TestQQNormalization:
    """QQ OneBot v11 消息标准化测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_normalize_qq_text_array_format(self) -> None:
        """QQ Array 格式文本消息。"""
        raw = {
            "user_id": 123456789,
            "message_id": 42,
            "message_type": "private",
            "message": [
                {"type": "text", "data": {"text": "Hello QQ"}},
            ],
            "time": 1700000000,
            "self_id": 987654321,
        }
        result = self.normalizer.normalize("qq", raw)

        assert result.channel_type == "qq"
        assert result.content == "Hello QQ"
        assert result.content_type == "text"
        assert result.unified_user_id == "qq:123456789"

    def test_normalize_qq_multiple_segments(self) -> None:
        """QQ 多段消息（文本 + @）。"""
        raw = {
            "user_id": 111222333,
            "message_id": 43,
            "message": [
                {"type": "at", "data": {"qq": "987654321"}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert "@987654321" in result.content
        assert "你好" in result.content

    def test_normalize_qq_image_only_array(self) -> None:
        """QQ 纯图片消息（Array 格式）→ content_type=image。"""
        raw = {
            "user_id": 111222333,
            "message_id": 44,
            "message": [
                {"type": "image", "data": {"file": "image.jpg"}},
            ],
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert result.content_type == "image"
        assert result.content == "[图片]"

    def test_normalize_qq_string_format(self) -> None:
        """QQ 字符串格式消息。"""
        raw = {
            "user_id": 444555666,
            "message_id": 45,
            "message": "Hello from string",
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert result.content == "Hello from string"

    def test_normalize_qq_string_with_cq_code(self) -> None:
        """QQ CQ 码字符串 → 移除 CQ 码保留纯文本。"""
        raw = {
            "user_id": 444555666,
            "message_id": 46,
            "message": "[CQ:at,qq=123] 请帮我查看 [CQ:image,file=a.jpg]",
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert "[CQ:" not in result.content
        assert "请帮我查看" in result.content

    def test_normalize_qq_image_cq_code(self) -> None:
        """QQ 图片 CQ 码 → content_type=image。"""
        raw = {
            "user_id": 444555666,
            "message_id": 47,
            "message": "[CQ:image,file=photo.jpg]",
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert result.content_type == "image"

    def test_normalize_qq_metadata_includes_group(self) -> None:
        """QQ 群消息包含 group_id。"""
        raw = {
            "user_id": 111222333,
            "message_id": 48,
            "message": [{"type": "text", "data": {"text": "群消息"}}],
            "time": 1700000000,
            "group_id": 999888777,
            "message_type": "group",
            "sender": {"nickname": "测试用户"},
        }
        result = self.normalizer.normalize("qq", raw)
        assert result.metadata["group_id"] == 999888777
        assert result.metadata["nickname"] == "测试用户"

    def test_normalize_qq_invalid_timestamp(self) -> None:
        """QQ 无效时间戳 → 使用当前时间。"""
        raw = {
            "user_id": 111222333,
            "message_id": 49,
            "message": "test",
            "time": "invalid",
        }
        before = time.time()
        result = self.normalizer.normalize("qq", raw)
        after = time.time()
        assert before <= result.timestamp <= after

    def test_normalize_qq_user_id_converted_to_string(self) -> None:
        """QQ user_id (int) 转为字符串。"""
        raw = {
            "user_id": 12345,
            "message_id": 50,
            "message": "test",
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)
        assert result.channel_user_id == "12345"

    # ── QQ 反标准化 ──

    def test_denormalize_qq_response(self) -> None:
        """UnifiedResponse → QQ OneBot 发送格式。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="qq",
            content="Hi from bot",
            content_type="text",
        )
        result = self.normalizer.denormalize("qq", resp)
        assert result["message_type"] == "private"
        assert isinstance(result["message"], list)
        assert result["message"][0]["type"] == "text"
        assert result["message"][0]["data"]["text"] == "Hi from bot"


class TestCustomChannelRegistration:
    """自定义渠道动态注册测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_register_and_normalize_custom_channel(self) -> None:
        """注册自定义渠道并标准化消息。"""
        def custom_normalize(raw: dict) -> UnifiedMessage:
            return UnifiedMessage(
                message_id=raw.get("id", "custom-001"),
                channel_type="custom",
                channel_user_id=raw.get("user", ""),
                unified_user_id=f"custom:{raw.get('user', 'unknown')}",
                content=raw.get("text", ""),
                content_type="text",
                raw_message=raw,
                timestamp=time.time(),
            )

        def custom_denormalize(response: UnifiedResponse) -> dict:
            return {"type": "custom_response", "body": response.content}

        self.normalizer.register("custom", custom_normalize, custom_denormalize)

        raw = {"id": "msg-c-001", "user": "alice", "text": "Hello custom"}
        result = self.normalizer.normalize("custom", raw)

        assert result.channel_type == "custom"
        assert result.content == "Hello custom"
        assert result.unified_user_id == "custom:alice"

    def test_register_and_denormalize_custom_channel(self) -> None:
        """注册自定义渠道并反标准化响应。"""
        def custom_normalize(raw: dict) -> UnifiedMessage:
            return UnifiedMessage(
                message_id="x", channel_type="custom", channel_user_id="",
                unified_user_id="custom:unknown", content="", content_type="text",
                raw_message={}, timestamp=0,
            )

        def custom_denormalize(response: UnifiedResponse) -> dict:
            return {"payload": response.content}

        self.normalizer.register("custom", custom_normalize, custom_denormalize)

        resp = UnifiedResponse(
            message_id="msg-x", channel_type="custom",
            content="Custom response", content_type="text",
        )
        result = self.normalizer.denormalize("custom", resp)
        assert result["payload"] == "Custom response"

    def test_overwrite_builtin_channel(self) -> None:
        """覆盖内置渠道的标准化器。"""
        call_count = {"n": 0}

        def custom_feishu(raw: dict) -> UnifiedMessage:
            call_count["n"] += 1
            return UnifiedMessage(
                message_id="override", channel_type="feishu",
                channel_user_id="", unified_user_id="feishu:override",
                content="overridden", content_type="text",
                raw_message=raw, timestamp=0,
            )

        def noop_denorm(resp: UnifiedResponse) -> dict:
            return {}

        self.normalizer.register("feishu", custom_feishu, noop_denorm)

        result = self.normalizer.normalize("feishu", {"any": "data"})
        assert call_count["n"] == 1
        assert result.content == "overridden"
