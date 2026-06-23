"""多通道接入模块补充测试。

覆盖需求文档中缺失/不足的 AC：
- AC-CH-01: 消息格式转换（ChannelMessage→UnifiedIncomingMessage）
- QQ / 企微适配器消息标准化
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse


# ============================================================
# AC-CH-01: 消息格式转换 — QQ 适配器
# ============================================================


class TestQQMessageNormalize:
    """QQ 渠道消息标准化测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_normalize_qq_text_message(self) -> None:
        """QQ OneBot 文本消息 → UnifiedMessage。"""
        raw = {
            "post_type": "message",
            "message_type": "private",
            "sub_type": "friend",
            "user_id": 123456789,
            "message": "Hello from QQ",
            "raw_message": "Hello from QQ",
            "message_id": "msg-qq-001",
            "self_id": 987654321,
            "time": 1700000000,
        }
        result = self.normalizer.normalize("qq", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "qq"
        assert result.channel_user_id == "123456789"
        assert result.unified_user_id == "qq:123456789"
        assert result.content == "Hello from QQ"
        assert result.content_type == "text"


# ============================================================
# AC-CH-01: 消息格式转换 — 企业微信适配器
# ============================================================


class TestWecomMessageNormalize:
    """企业微信渠道消息标准化测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_normalize_wecom_text_message(self) -> None:
        """企微文本消息 → UnifiedMessage。"""
        raw = {
            "ToUserName": "corp_id",
            "FromUserName": "user_wecom_001",
            "CreateTime": 1700000000,
            "MsgType": "text",
            "Content": "Hello from Wecom",
            "MsgId": "msg-wecom-001",
        }
        result = self.normalizer.normalize("wecom", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "wecom"
        assert result.channel_user_id == "user_wecom_001"
        assert result.unified_user_id == "wecom:user_wecom_001"
        assert result.content == "Hello from Wecom"
        assert result.content_type == "text"


# ============================================================
# AC-CH-01: 消息格式转换 — 反标准化（出站）
# ============================================================


class TestDenormalizeChannels:
    """验证统一格式 → 各渠道发送格式的反标准化。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    def test_denormalize_qq_text(self) -> None:
        """UnifiedResponse → QQ 发送格式。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="qq",
            content="Hi from bot",
            content_type="text",
            card_config=None,
            metadata={},
        )
        result = self.normalizer.denormalize("qq", resp)
        # QQ 的发送格式应包含消息内容
        assert result is not None

    def test_denormalize_wecom_text(self) -> None:
        """UnifiedResponse → 企微发送格式。"""
        resp = UnifiedResponse(
            message_id="msg-002",
            channel_type="wecom",
            content="Hi from bot",
            content_type="text",
            card_config=None,
            metadata={},
        )
        result = self.normalizer.denormalize("wecom", resp)
        assert result is not None


# ============================================================
# AC-CH-01: 统一消息格式完整性验证
# ============================================================


class TestUnifiedMessageFields:
    """验证 UnifiedMessage 核心字段完整性。"""

    def test_unified_message_has_required_fields(self) -> None:
        """UnifiedMessage 必须包含所有核心字段。"""
        msg = UnifiedMessage(
            message_id="msg-001",
            channel_type="feishu",
            channel_user_id="ou_xxx",
            unified_user_id="feishu:ou_xxx",
            content="Hello",
            content_type="text",
            raw_message={},
            timestamp=1700000000.0,
            metadata={},
        )

        # 核心字段必须存在
        assert msg.message_id is not None
        assert msg.channel_type is not None
        assert msg.channel_user_id is not None
        assert msg.unified_user_id is not None
        assert msg.content is not None
        assert msg.content_type is not None

    def test_unified_user_id_format(self) -> None:
        """unified_user_id 格式应为 channel:user_id。"""
        msg = UnifiedMessage(
            message_id="msg-001",
            channel_type="dingtalk",
            channel_user_id="user_001",
            unified_user_id="dingtalk:user_001",
            content="test",
            content_type="text",
            raw_message={},
            timestamp=1700000000.0,
            metadata={},
        )
        assert ":" in msg.unified_user_id
        parts = msg.unified_user_id.split(":")
        assert parts[0] == "dingtalk"
        assert parts[1] == "user_001"
