"""消息标准化器。

负责渠道格式 ↔ 统一格式的双向转换。使用策略模式，
为每个渠道注册独立的 Normalizer 函数，支持轻松扩展新渠道。

核心方法：
- normalize: 渠道原始消息 → UnifiedMessage
- denormalize: UnifiedResponse → 渠道发送格式
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from channels.gateway.unified_types import UnifiedMessage, UnifiedResponse

logger = logging.getLogger(__name__)

# 类型别名
NormalizeFunc = Callable[[dict[str, Any]], UnifiedMessage]
DenormalizeFunc = Callable[[UnifiedResponse], dict[str, Any]]


class MessageNormalizer:
    """消息标准化器。

    管理各渠道的消息标准化和反标准化策略，支持动态注册新渠道。

    Example::

        normalizer = MessageNormalizer()
        msg = normalizer.normalize("feishu", raw_feishu_event)
        resp_dict = normalizer.denormalize("feishu", unified_response)
    """

    def __init__(self) -> None:
        """初始化并注册内置渠道的标准化器。"""
        self._normalizers: dict[str, NormalizeFunc] = {}
        self._denormalizers: dict[str, DenormalizeFunc] = {}
        self._register_builtin_channels()

    def _register_builtin_channels(self) -> None:
        """注册内置支持的渠道标准化器。"""
        self._normalizers["feishu"] = self._normalize_feishu
        self._denormalizers["feishu"] = self._denormalize_feishu
        self._normalizers["dingtalk"] = self._normalize_dingtalk
        self._denormalizers["dingtalk"] = self._denormalize_dingtalk
        self._normalizers["wecom"] = self._normalize_wecom
        self._denormalizers["wecom"] = self._denormalize_wecom
        self._normalizers["qq"] = self._normalize_qq
        self._denormalizers["qq"] = self._denormalize_qq

    def register(
        self,
        channel_type: str,
        normalize_func: NormalizeFunc,
        denormalize_func: DenormalizeFunc,
    ) -> None:
        """注册新渠道的标准化器。

        Args:
            channel_type: 渠道类型标识
            normalize_func: 标准化函数 raw → UnifiedMessage
            denormalize_func: 反标准化函数 UnifiedResponse → dict
        """
        self._normalizers[channel_type] = normalize_func
        self._denormalizers[channel_type] = denormalize_func
        logger.info("Registered normalizer for channel: %s", channel_type)

    def normalize(self, channel_type: str, raw_message: dict[str, Any]) -> UnifiedMessage:
        """将渠道原始消息标准化为 UnifiedMessage。

        Args:
            channel_type: 渠道类型
            raw_message: 渠道原始消息字典

        Returns:
            统一格式的 UnifiedMessage

        Raises:
            ValueError: 不支持的渠道类型
        """
        normalizer = self._normalizers.get(channel_type)
        if normalizer is None:
            raise ValueError(f"Unsupported channel type: {channel_type}")
        return normalizer(raw_message)

    def denormalize(self, channel_type: str, response: UnifiedResponse) -> dict[str, Any]:
        """将 UnifiedResponse 反标准化为渠道发送格式。

        Args:
            channel_type: 渠道类型
            response: 统一响应

        Returns:
            渠道特定的发送格式字典

        Raises:
            ValueError: 不支持的渠道类型
        """
        denormalizer = self._denormalizers.get(channel_type)
        if denormalizer is None:
            raise ValueError(f"Unsupported channel type: {channel_type}")
        return denormalizer(response)

    # ── 飞书标准化实现 ──────────────────────────────────

    @staticmethod
    def _normalize_feishu(raw: dict[str, Any]) -> UnifiedMessage:
        """飞书 im.message.receive_v1 事件 → UnifiedMessage。

        飞书消息事件格式参考：
        https://open.feishu.cn/document/server-docs/im-v1/message/events/receive

        Args:
            raw: 飞书事件体

        Returns:
            标准化后的 UnifiedMessage
        """
        event = raw.get("event", {})
        header = raw.get("header", {})
        sender = event.get("sender", {})
        sender_id = sender.get("sender_id", {})
        message = event.get("message", {})

        open_id = sender_id.get("open_id", "")
        message_id = message.get("message_id", header.get("event_id", uuid.uuid4().hex))
        msg_type = message.get("message_type", "text")
        content_str = message.get("content", "{}")
        create_time = message.get("create_time", "0")

        # 解析消息内容
        content, content_type = _parse_feishu_content(msg_type, content_str)

        # 生成统一用户 ID
        unified_user_id = f"feishu:{open_id}" if open_id else "feishu:unknown"

        # 解析时间戳（飞书时间戳为毫秒）
        try:
            timestamp = int(create_time) / 1000.0 if create_time else time.time()
        except (ValueError, TypeError):
            timestamp = time.time()

        return UnifiedMessage(
            message_id=message_id,
            channel_type="feishu",
            channel_user_id=open_id,
            unified_user_id=unified_user_id,
            content=content,
            content_type=content_type,
            raw_message=raw,
            timestamp=timestamp,
            metadata={"event_type": header.get("event_type", "")},
        )

    @staticmethod
    def _denormalize_feishu(response: UnifiedResponse) -> dict[str, Any]:
        """UnifiedResponse → 飞书发送消息 API 格式。

        Args:
            response: 统一响应

        Returns:
            飞书发送消息 API 的请求体格式
        """
        if response.content_type == "card" and response.card_config:
            return {
                "msg_type": "interactive",
                "content": {"card": response.card_config},
            }
        return {
            "msg_type": "text",
            "content": {"text": response.content},
        }

    # ── 钉钉标准化实现 ──────────────────────────────────

    @staticmethod
    def _normalize_dingtalk(raw: dict[str, Any]) -> UnifiedMessage:
        """钉钉 Stream 消息事件 → UnifiedMessage。

        钉钉 Stream 消息格式参考：
        https://open.dingtalk.com/document/orgapp/stream-mode-protocol

        Args:
            raw: 钉钉消息事件体

        Returns:
            标准化后的 UnifiedMessage
        """
        sender_staff_id = raw.get("senderStaffId", "")
        sender_id = raw.get("senderId", "")
        msg_id = raw.get("messageId", uuid.uuid4().hex)
        msg_type = raw.get("msgtype", "text")
        create_at = raw.get("createAt", "0")

        # 提取文本内容
        content, content_type = _parse_dingtalk_content(msg_type, raw)

        unified_user_id = f"dingtalk:{sender_staff_id}" if sender_staff_id else "dingtalk:unknown"

        try:
            timestamp = int(create_at) / 1000.0 if create_at else time.time()
        except (ValueError, TypeError):
            timestamp = time.time()

        return UnifiedMessage(
            message_id=msg_id,
            channel_type="dingtalk",
            channel_user_id=sender_staff_id,
            unified_user_id=unified_user_id,
            content=content,
            content_type=content_type,
            raw_message=raw,
            timestamp=timestamp,
            metadata={
                "conversation_id": raw.get("conversationId", ""),
                "sender_id": sender_id,
            },
        )

    @staticmethod
    def _denormalize_dingtalk(response: UnifiedResponse) -> dict[str, Any]:
        """UnifiedResponse → 钉钉发送消息格式。

        钉钉支持 text 和 markdown 消息类型，卡片降级为 markdown。

        Args:
            response: 统一响应

        Returns:
            钉钉发送消息的请求体格式
        """
        if response.content_type == "card" and response.card_config:
            # 钉钉不支持飞书卡片，降级为 markdown
            card = response.card_config or {}
            header = card.get("header", {})
            title_raw = header.get("title", {})
            title = title_raw.get("content", "") if isinstance(title_raw, dict) else str(title_raw)

            text = response.content or title
            return {
                "msgtype": "markdown",
                "markdown": {
                    "title": title or "Message",
                    "text": text,
                },
            }
        return {
            "msgtype": "text",
            "text": {"content": response.content},
        }

    # ── 企业微信标准化实现 ──────────────────────────────────

    @staticmethod
    def _normalize_wecom(raw: dict[str, Any]) -> UnifiedMessage:
        """企业微信回调消息 → UnifiedMessage。

        企业微信回调消息为 XML 格式，经适配器解密后转为字典传入。

        Args:
            raw: 企业微信消息字典（XML 解析结果）

        Returns:
            标准化后的 UnifiedMessage
        """
        from_user = raw.get("FromUserName", "")
        msg_id = raw.get("MsgId", uuid.uuid4().hex)
        msg_type = raw.get("MsgType", "text")
        content = raw.get("Content", "")
        create_time = raw.get("CreateTime", "0")

        # 解析消息内容
        content, content_type = _parse_wecom_content(msg_type, content, raw)

        # 生成统一用户 ID
        unified_user_id = f"wecom:{from_user}" if from_user else "wecom:unknown"

        # 解析时间戳
        try:
            timestamp = int(create_time) if create_time else time.time()
        except (ValueError, TypeError):
            timestamp = time.time()

        return UnifiedMessage(
            message_id=msg_id,
            channel_type="wecom",
            channel_user_id=from_user,
            unified_user_id=unified_user_id,
            content=content,
            content_type=content_type,
            raw_message=raw,
            timestamp=timestamp,
            metadata={
                "agent_id": raw.get("AgentID", ""),
                "to_user": raw.get("ToUserName", ""),
            },
        )

    @staticmethod
    def _denormalize_wecom(response: UnifiedResponse) -> dict[str, Any]:
        """UnifiedResponse → 企业微信发送消息格式。

        企业微信支持 text 和 markdown 消息类型。

        Args:
            response: 统一响应

        Returns:
            企业微信发送消息的请求体格式
        """
        if response.content_type == "card" and response.card_config:
            # 企业微信不支持卡片，降级为 markdown
            card = response.card_config or {}
            header = card.get("header", {})
            title_raw = header.get("title", {})
            title = title_raw.get("content", "") if isinstance(title_raw, dict) else str(title_raw)

            text = response.content or title
            return {
                "msgtype": "markdown",
                "markdown": {
                    "content": f"**{title or 'Message'}**\n\n{text}",
                },
            }
        return {
            "msgtype": "text",
            "text": {"content": response.content},
        }

    # ── QQ 标准化实现 ──────────────────────────────────

    @staticmethod
    def _normalize_qq(raw: dict[str, Any]) -> UnifiedMessage:
        """QQ OneBot v11 消息事件 → UnifiedMessage。

        OneBot v11 消息格式参考：
        https://github.com/botuniverse/onebot-11/blob/master/message/segment.md

        Args:
            raw: OneBot v11 消息事件体

        Returns:
            标准化后的 UnifiedMessage
        """
        user_id = str(raw.get("user_id", ""))
        message_id = str(raw.get("message_id", uuid.uuid4().hex))
        message_type = raw.get("message_type", "private")
        msg_time = raw.get("time", 0)

        # 提取文本内容
        content, content_type = _parse_qq_content(raw)

        # 生成统一用户 ID
        unified_user_id = f"qq:{user_id}" if user_id else "qq:unknown"

        # 解析时间戳
        try:
            timestamp = float(msg_time) if msg_time else time.time()
        except (ValueError, TypeError):
            timestamp = time.time()

        # 构建元数据
        metadata: dict[str, Any] = {
            "message_type": message_type,
            "self_id": str(raw.get("self_id", "")),
        }
        if raw.get("group_id"):
            metadata["group_id"] = raw.get("group_id")
        if raw.get("sub_type"):
            metadata["sub_type"] = raw.get("sub_type")
        sender = raw.get("sender", {})
        if sender.get("nickname"):
            metadata["nickname"] = sender.get("nickname")

        return UnifiedMessage(
            message_id=message_id,
            channel_type="qq",
            channel_user_id=user_id,
            unified_user_id=unified_user_id,
            content=content,
            content_type=content_type,
            raw_message=raw,
            timestamp=timestamp,
            metadata=metadata,
        )

    @staticmethod
    def _denormalize_qq(response: UnifiedResponse) -> dict[str, Any]:
        """UnifiedResponse → QQ OneBot 发送消息格式。

        QQ 支持文本和 Array 格式消息段。

        Args:
            response: 统一响应

        Returns:
            OneBot 发送消息的请求体格式
        """
        # 构建消息段
        message = [{"type": "text", "data": {"text": response.content}}]

        return {
            "message_type": "private",
            "message": message,
        }


# ── 内部辅助函数 ──────────────────────────────────────


def _parse_feishu_content(msg_type: str, content_str: str) -> tuple[str, str]:
    """解析飞书消息内容。

    Args:
        msg_type: 飞书消息类型
        content_str: 飞书消息 content JSON 字符串

    Returns:
        (文本内容, 标准化内容类型) 元组
    """
    type_mapping = {
        "text": "text",
        "image": "image",
        "file": "file",
        "post": "text",
        "interactive": "text",
    }

    content_type = type_mapping.get(msg_type, "text")

    try:
        parsed = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        # JSON 解析失败，降级为原始字符串
        return content_str, "text"

    if msg_type == "text":
        return parsed.get("text", ""), content_type
    if msg_type == "image":
        return parsed.get("image_key", ""), content_type
    if msg_type == "file":
        return parsed.get("file_name", parsed.get("file_key", "")), content_type
    if msg_type == "post":
        # 富文本提取纯文本
        return _extract_feishu_post_text(parsed), "text"
    # 其他类型降级为文本
    return parsed.get("text", json.dumps(parsed)), "text"


def _extract_feishu_post_text(post_data: dict[str, Any]) -> str:
    """从飞书富文本 post 中提取纯文本。

    Args:
        post_data: 飞书 post content 解析后的字典

    Returns:
        拼接后的纯文本
    """
    parts: list[str] = []
    content = post_data.get("content", [])
    for line in content:
        for element in line:
            text = element.get("text", "")
            if text:
                parts.append(text)
    return " ".join(parts)


def _parse_dingtalk_content(msg_type: str, raw: dict[str, Any]) -> tuple[str, str]:
    """解析钉钉消息内容。

    Args:
        msg_type: 钉钉消息类型
        raw: 钉钉原始消息

    Returns:
        (文本内容, 标准化内容类型) 元组
    """
    type_mapping = {
        "text": "text",
        "richText": "text",
        "picture": "image",
        "file": "file",
    }

    content_type = type_mapping.get(msg_type, "text")

    if msg_type == "text":
        text_data = raw.get("text", {})
        return text_data.get("content", ""), content_type
    if msg_type == "richText":
        rich_data = raw.get("richText", {})
        return rich_data.get("content", ""), content_type
    if msg_type == "picture":
        pic_data = raw.get("picture", {})
        return pic_data.get("downloadCode", ""), "image"
    if msg_type == "file":
        file_data = raw.get("file", {})
        return file_data.get("fileName", ""), "file"

    # 不支持的类型降级为文本
    return str(raw.get(msg_type, "")), "text"


def _parse_wecom_content(  # noqa: PLR0911
    msg_type: str, content: str, raw: dict[str, Any]
) -> tuple[str, str]:
    """解析企业微信消息内容。

    Args:
        msg_type: 企业微信消息类型
        content: 消息 Content 字段
        raw: 完整消息字典

    Returns:
        (文本内容, 标准化内容类型) 元组
    """
    type_mapping: dict[str, str] = {
        "text": "text",
        "image": "image",
        "voice": "text",
        "video": "text",
        "shortvideo": "text",
        "location": "text",
        "link": "text",
    }

    content_type = type_mapping.get(msg_type, "text")

    if msg_type == "text":
        return content, content_type
    if msg_type == "image":
        return raw.get("PicUrl", ""), "image"
    if msg_type == "voice":
        recognition = raw.get("Recognition", "")
        return recognition if recognition else "[语音]", content_type
    if msg_type in {"video", "shortvideo"}:
        return "[视频]", content_type
    if msg_type == "location":
        label = raw.get("Label", "")
        return f"[位置] {label}" if label else "[位置]", content_type
    if msg_type == "link":
        return raw.get("Description", content), content_type

    # 不支持的类型降级为文本
    return content or str(raw.get(msg_type, "")), "text"


def _parse_qq_content(raw: dict[str, Any]) -> tuple[str, str]:
    """解析 QQ OneBot v11 消息内容。

    支持 OneBot v11 的两种消息格式：
    - Array 格式：message 为消息段数组 [{"type": "text", "data": {"text": "..."}}]
    - 字符串格式：message 为纯文本或 CQ 码字符串

    Args:
        raw: OneBot v11 消息事件数据

    Returns:
        (文本内容, 标准化内容类型) 元组
    """
    import re  # noqa: PLC0415

    message = raw.get("message", "")

    # 默认内容类型映射
    content_type = "text"

    if isinstance(message, list):
        # Array 格式：提取所有 text 段的文本
        parts: list[str] = []
        has_image = False
        for segment in message:
            if not isinstance(segment, dict):
                continue
            seg_type = segment.get("type", "")
            if seg_type == "text":
                text = segment.get("data", {}).get("text", "")
                if text:
                    parts.append(text)
            elif seg_type == "image":
                has_image = True
            elif seg_type == "at":
                qq = segment.get("data", {}).get("qq", "")
                if qq:
                    parts.append(f"@{qq}")

        if not parts and has_image:
            content_type = "image"
            return "[图片]", content_type

        return " ".join(parts) if parts else "", content_type

    if isinstance(message, str):
        # 字符串格式：移除 CQ 码，保留纯文本
        # 检测是否包含图片 CQ 码
        if "[CQ:image" in message:
            content_type = "image"

        # CQ 码格式：[CQ:type,key=value,...]
        text = re.sub(r"\[CQ:[^\]]+\]", "", message)
        return text.strip() if text.strip() else message, content_type

    # 降级处理
    return str(message), content_type
