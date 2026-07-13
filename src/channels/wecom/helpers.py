"""企业微信通道辅助函数。"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any

logger = logging.getLogger(__name__)


def _extract_encrypt(xml_str: str) -> str:
    """从加密 XML 中提取 Encrypt 字段。

    Args:
        xml_str: 加密消息 XML

    Returns:
        Encrypt 字段内容
    """
    try:
        root = ET.fromstring(xml_str)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is not None and encrypt_node.text:
            return encrypt_node.text
    except ET.ParseError:
        pass
    return ""


def _parse_message_xml(xml_str: str) -> dict[str, Any]:
    """解析企业微信消息 XML 为字典。

    Args:
        xml_str: 解密后的消息 XML

    Returns:
        消息字段字典
    """
    try:
        root = ET.fromstring(xml_str)
        result: dict[str, Any] = {}
        for child in root:
            result[child.tag] = child.text or ""
        return result
    except ET.ParseError:
        logger.warning("Failed to parse WeCom message XML")
        return {}


def _extract_wecom_text(  # noqa: PLR0911
    msg_type: str,
    content: str,
    raw: dict[str, Any],
) -> str:
    """从企业微信消息中提取文本。

    Args:
        msg_type: 消息类型
        content: 消息内容字段
        raw: 完整消息字典

    Returns:
        提取的纯文本
    """
    if msg_type == "text":
        return content
    if msg_type == "image":
        return raw.get("PicUrl", "[图片]")
    if msg_type == "voice":
        recognition = raw.get("Recognition", "")
        return recognition if recognition else "[语音]"
    if msg_type in {"video", "shortvideo"}:
        return "[视频]"
    if msg_type == "location":
        label = raw.get("Label", "")
        return f"[位置] {label}" if label else "[位置]"
    if msg_type == "link":
        return raw.get("Description", content)
    # 其他类型降级
    return content or str(raw)
