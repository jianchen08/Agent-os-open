"""QQ 通道辅助函数。"""

from __future__ import annotations

import re
from typing import Any


def _extract_qq_text(raw: dict[str, Any]) -> str:
    """从 QQ 消息中提取纯文本。

    支持 OneBot v11 的两种消息格式：
    - Array 格式：message 为消息段数组
    - 字符串格式：message 为纯文本或 CQ 码字符串

    Args:
        raw: OneBot v11 消息事件数据

    Returns:
        提取的纯文本
    """
    message = raw.get("message", "")

    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if isinstance(segment, dict) and segment.get("type") == "text":
                text = segment.get("data", {}).get("text", "")
                if text:
                    parts.append(text)
        return " ".join(parts) if parts else ""

    if isinstance(message, str):
        text = re.sub(r"\[CQ:[^\]]+\]", "", message)
        return text.strip()

    return str(message)
