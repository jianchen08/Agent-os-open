"""多渠道消息网关核心组件测试。

测试 ChannelGateway、MessageNormalizer、SessionBridge 和 UnifiedTypes
的核心功能，覆盖消息标准化、反标准化、会话管理和网关路由。
"""

from __future__ import annotations

import sys
import os
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# 确保 src 在 sys.path 中
_src = os.path.join(os.path.dirname(__file__), "..", "..", "src")
if _src not in sys.path:
    sys.path.insert(0, os.path.abspath(_src))

from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse
from channels.gateway.message_normalizer import MessageNormalizer
from channels.gateway.session_bridge import SessionBridge
from channels.gateway.channel_gateway import ChannelGateway


# ═══════════════════════════════════════════════════════════
# MessageNormalizer 测试
# ═══════════════════════════════════════════════════════════


class TestMessageNormalizer:
    """MessageNormalizer 标准化/反标准化测试。"""

    def setup_method(self) -> None:
        self.normalizer = MessageNormalizer()

    # ── 飞书消息标准化 ──────────────────────────────────

    def test_normalize_feishu_text_message(self) -> None:
        """飞书文本消息 → UnifiedMessage。"""
        raw = {
            "header": {"event_id": "evt-001", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_abc"}},
                "message": {
                    "message_id": "msg-001",
                    "message_type": "text",
                    "content": '{"text":"Hello Feishu"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "feishu"
        assert result.channel_user_id == "ou_abc"
        assert result.unified_user_id == "feishu:ou_abc"
        assert result.content == "Hello Feishu"
        assert result.content_type == "text"
        assert result.raw_message is raw

    def test_normalize_feishu_image_message(self) -> None:
        """飞书图片消息解析。"""
        raw = {
            "header": {"event_id": "evt-002", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_xyz"}},
                "message": {
                    "message_id": "msg-002",
                    "message_type": "image",
                    "content": '{"image_key":"img_abc123"}',
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content_type == "image"
        assert result.content == "img_abc123"

    def test_normalize_feishu_post_message(self) -> None:
        """飞书富文本消息提取纯文本。"""
        raw = {
            "header": {"event_id": "evt-003", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_post"}},
                "message": {
                    "message_id": "msg-003",
                    "message_type": "post",
                    "content": json.dumps({
                        "content": [
                            [{"text": "Hello "}, {"text": "World"}],
                            [{"text": "Second line"}],
                        ]
                    }),
                    "create_time": "1700000000000",
                },
            },
        }
        result = self.normalizer.normalize("feishu", raw)
        assert result.content_type == "text"
        assert "Hello" in result.content
        assert "World" in result.content
        assert "Second line" in result.content

    # ── 钉钉消息标准化 ──────────────────────────────────

    def test_normalize_dingtalk_text_message(self) -> None:
        """钉钉文本消息 → UnifiedMessage。"""
        raw = {
            "conversationId": "cid-001",
            "senderStaffId": "user_dt_001",
            "senderId": "sender-id-001",
            "msgtype": "text",
            "text": {"content": "Hello DingTalk"},
            "createAt": "1700000000000",
            "messageId": "msg-dt-001",
        }
        result = self.normalizer.normalize("dingtalk", raw)

        assert isinstance(result, UnifiedMessage)
        assert result.channel_type == "dingtalk"
        assert result.channel_user_id == "user_dt_001"
        assert result.unified_user_id == "dingtalk:user_dt_001"
        assert result.content == "Hello DingTalk"
        assert result.content_type == "text"

    # ── 不支持的渠道 ──────────────────────────────────

    def test_normalize_unsupported_channel(self) -> None:
        """不支持的渠道抛出 ValueError。"""
        with pytest.raises(ValueError, match="Unsupported channel"):
            self.normalizer.normalize("slack", {"data": "irrelevant"})

    # ── 反标准化 ──────────────────────────────────────

    def test_denormalize_feishu_text(self) -> None:
        """UnifiedResponse → 飞书文本发送格式。"""
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

    def test_denormalize_feishu_card(self) -> None:
        """UnifiedResponse → 飞书卡片发送格式。"""
        card = {"header": {"title": "Card Title"}, "elements": []}
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

    def test_denormalize_dingtalk_text(self) -> None:
        """UnifiedResponse → 钉钉文本发送格式。"""
        resp = UnifiedResponse(
            message_id="msg-002",
            channel_type="dingtalk",
            content="Hi from dingtalk",
            content_type="text",
            card_config=None,
            metadata={},
        )
        result = self.normalizer.denormalize("dingtalk", resp)
        assert result["msgtype"] == "text"
        assert result["text"]["content"] == "Hi from dingtalk"

    def test_denormalize_dingtalk_card_fallback(self) -> None:
        """卡片降级为 markdown。"""
        card = {"header": {"title": {"content": "Card Title"}}, "elements": []}
        resp = UnifiedResponse(
            message_id="msg-002",
            channel_type="dingtalk",
            content="Card content",
            content_type="card",
            card_config=card,
            metadata={},
        )
        result = self.normalizer.denormalize("dingtalk", resp)
        assert result["msgtype"] == "markdown"
        assert "Card content" in result["markdown"]["text"]


# ═══════════════════════════════════════════════════════════
# SessionBridge 测试
# ═══════════════════════════════════════════════════════════


class TestSessionBridge:
    """SessionBridge 跨通道会话桥接测试。"""

    def setup_method(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.bridge = SessionBridge(storage_path=Path(self.tmpdir))

    def teardown_method(self) -> None:
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_session(self) -> None:
        """创建新会话。"""
        sid = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        assert isinstance(sid, str)
        assert len(sid) > 0

    def test_get_existing_session(self) -> None:
        """获取已有会话（幂等）。"""
        sid1 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid2 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        assert sid1 == sid2

    def test_switch_channel(self) -> None:
        """切换活跃通道。"""
        self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        self.bridge.switch_channel("feishu:ou_001", "dingtalk")
        active = self.bridge.get_active_channel("feishu:ou_001")
        assert active == "dingtalk"

    def test_get_active_channel(self) -> None:
        """获取活跃通道。"""
        self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        assert self.bridge.get_active_channel("feishu:ou_001") == "feishu"
        assert self.bridge.get_active_channel("unknown:user") == ""

    def test_session_sharing_across_channels(self) -> None:
        """同一用户从不同通道获取同一 session_id。"""
        sid_feishu = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid_dingtalk = self.bridge.get_or_create_session("feishu:ou_001", "dingtalk")
        assert sid_feishu == sid_dingtalk

    def test_persistence_and_restore(self) -> None:
        """持久化和恢复。"""
        sid = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        self.bridge.switch_channel("feishu:ou_001", "dingtalk")

        bridge2 = SessionBridge(storage_path=Path(self.tmpdir))
        sid2 = bridge2.get_or_create_session("feishu:ou_001", "feishu")
        assert sid2 == sid
        assert bridge2.get_active_channel("feishu:ou_001") == "dingtalk"

    def test_different_users_separate_sessions(self) -> None:
        """不同用户应有不同的会话。"""
        sid1 = self.bridge.get_or_create_session("feishu:ou_001", "feishu")
        sid2 = self.bridge.get_or_create_session("feishu:ou_002", "feishu")
        assert sid1 != sid2


# ═══════════════════════════════════════════════════════════
# ChannelGateway 测试
# ═══════════════════════════════════════════════════════════


class TestChannelGateway:
    """ChannelGateway 网关主入口测试。"""

    def setup_method(self) -> None:
        self.gateway = ChannelGateway()

    def test_register_adapter(self) -> None:
        """注册通道适配器。"""
        mock_adapter = MagicMock()
        mock_adapter.channel_type = "feishu"
        self.gateway.register_adapter("feishu", mock_adapter)
        assert "feishu" in self.gateway._adapters

    def test_register_duplicate_adapter(self) -> None:
        """重复注册抛出 ValueError。"""
        mock_adapter = MagicMock()
        self.gateway.register_adapter("feishu", mock_adapter)
        with pytest.raises(ValueError, match="already registered"):
            self.gateway.register_adapter("feishu", mock_adapter)

    @pytest.mark.asyncio
    async def test_handle_message_normalization_error(self) -> None:
        """消息标准化失败时的错误处理（不支持的渠道不抛异常）。"""
        handler = AsyncMock()
        self.gateway.on_pipeline_request = handler
        # 不支持的渠道，normalize 会抛 ValueError，handle_message 内部捕获
        await self.gateway.handle_message("slack", {"data": "test"})
        handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_response_no_adapter(self) -> None:
        """无适配器时的日志警告（不抛异常）。"""
        response = UnifiedResponse(
            message_id="msg-001",
            channel_type="nonexistent",
            content="test",
            content_type="text",
            card_config=None,
            metadata={},
        )
        # 不应抛异常，只记录日志
        await self.gateway.send_response(response)

    @pytest.mark.asyncio
    async def test_handle_message_normalizes_and_routes(self) -> None:
        """消息处理流程：标准化 → 获取会话 → 创建状态。"""
        handler = AsyncMock()
        self.gateway.on_pipeline_request = handler

        raw_msg = {
            "header": {"event_id": "evt-1", "event_type": "im.message.receive_v1"},
            "event": {
                "sender": {"sender_id": {"open_id": "ou_test"}},
                "message": {
                    "message_id": "msg-1",
                    "message_type": "text",
                    "content": '{"text":"hello gateway"}',
                    "create_time": "1700000000000",
                },
            },
        }
        await self.gateway.handle_message("feishu", raw_msg)

        handler.assert_called_once()
        state = handler.call_args[0][0]
        assert state["user_input"] == "hello gateway"
        assert state["_channel_type"] == "feishu"
        assert state["_channel_user_id"] == "ou_test"
        assert state["_unified_user_id"] == "feishu:ou_test"

    @pytest.mark.asyncio
    async def test_send_response_with_adapter(self) -> None:
        """发送响应到指定渠道。"""
        mock_adapter = MagicMock()
        mock_output = AsyncMock()
        mock_adapter.output_adapter = mock_output
        self.gateway.register_adapter("feishu", mock_adapter)

        response = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="Response text",
            content_type="text",
            card_config=None,
            metadata={},
        )
        await self.gateway.send_response(response)
        mock_output.send.assert_called_once()


# ═══════════════════════════════════════════════════════════
# UnifiedTypes 测试
# ═══════════════════════════════════════════════════════════


class TestUnifiedMessage:
    """UnifiedMessage 数据类测试。"""

    def test_unified_message_creation(self) -> None:
        """验证 UnifiedMessage 字段。"""
        msg = UnifiedMessage(
            message_id="msg-001",
            channel_type="feishu",
            channel_user_id="ou_xxx",
            unified_user_id="feishu:ou_xxx",
            content="Hello",
            content_type="text",
            raw_message={"event": {"message": {"content": "Hello"}}},
            timestamp=time.time(),
            metadata={"key": "value"},
        )
        assert msg.message_id == "msg-001"
        assert msg.channel_type == "feishu"
        assert msg.channel_user_id == "ou_xxx"
        assert msg.unified_user_id == "feishu:ou_xxx"
        assert msg.content == "Hello"
        assert msg.content_type == "text"
        assert isinstance(msg.raw_message, dict)
        assert msg.metadata["key"] == "value"


class TestUnifiedResponse:
    """UnifiedResponse 数据类测试。"""

    def test_unified_response_creation(self) -> None:
        """验证 UnifiedResponse 字段。"""
        resp = UnifiedResponse(
            message_id="msg-001",
            channel_type="feishu",
            content="Hi there",
            content_type="text",
            card_config=None,
            metadata={"template": "text_card"},
        )
        assert resp.message_id == "msg-001"
        assert resp.channel_type == "feishu"
        assert resp.content == "Hi there"
        assert resp.content_type == "text"
        assert resp.card_config is None
        assert resp.metadata["template"] == "text_card"
