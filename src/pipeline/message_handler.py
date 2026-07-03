"""管道消息解析转换层。

提供前端原始 JSON 消息到标准内部消息对象的转换。
这是前端消息进入系统的唯一转换入口。

公共接口:
    parse_frontend_message: 前端原始 JSON → PipelineMessage
    MessageParseError: 解析失败异常
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.message_types import (
    MessageSource,
    MessageType,
    PipelineMessage,
)

logger = logging.getLogger(__name__)

__all__ = ["parse_frontend_message", "MessageParseError"]


class MessageParseError(Exception):
    """消息解析失败异常。

    Attributes:
        reason: 失败原因描述
        raw_data: 原始数据（调试用）
    """

    def __init__(self, reason: str, raw_data: dict | None = None) -> None:
        self.reason = reason
        self.raw_data = raw_data
        super().__init__(f"消息解析失败: {reason}")


# 前端 type 字符串 → 内部 MessageType 映射表
_TYPE_MAPPING: dict[str, MessageType] = {
    "user_input": MessageType.CHAT,
    "interaction_response": MessageType.INTERACTION_RESPONSE,
    "stop_generation": MessageType.CONTROL,
    "resume_action": MessageType.CONTROL,
}


def parse_frontend_message(raw_data: dict[str, Any]) -> PipelineMessage:
    """将前端原始 JSON 消息解析为标准内部消息对象。

    这是前端消息进入系统的唯一转换入口。
    所有前端消息必须经过此函数转换为 PipelineMessage 后
    才能注入管道。

    Args:
        raw_data: 前端 WebSocket 发送的原始 JSON 字典

    Returns:
        标准化的 PipelineMessage 对象

    Raises:
        MessageParseError: 消息格式不合法时抛出

    解析规则:
        - type="user_input" → MessageType.CHAT
        - type="interaction_response" → MessageType.INTERACTION_RESPONSE
        - type="stop_generation"/"resume_action" → MessageType.CONTROL
        - 其他 type → MessageType.CHAT（兜底）
    """
    if not isinstance(raw_data, dict):
        raise MessageParseError("消息必须是 JSON 对象", raw_data)

    msg_type_str = raw_data.get("type", "")
    if not msg_type_str:
        raise MessageParseError("缺少 type 字段", raw_data)

    # 提取数据区域：兼容平铺和 envelope 两种格式
    data = raw_data.get("data", raw_data)
    if not isinstance(data, dict):
        data = raw_data

    content = data.get("content", "")
    pipeline_id = data.get("pipeline_id", "")
    thread_id = data.get("thread_id", "")
    client_message_id = data.get("client_message_id", "")

    # 类型映射
    message_type = _TYPE_MAPPING.get(msg_type_str, MessageType.CHAT)

    # 消息来源判断
    source = MessageSource.USER
    metadata_source = (data.get("metadata", {}) or {}).get("source", "")
    if metadata_source == "system":
        source = MessageSource.SYSTEM
    elif metadata_source == "trigger":
        source = MessageSource.TRIGGER

    return PipelineMessage(
        type=message_type,
        content=content,
        source=source,
        pipeline_id=pipeline_id,
        thread_id=thread_id,
        client_message_id=client_message_id,
        metadata=dict(data)
        if message_type in (MessageType.INTERACTION_RESPONSE, MessageType.CONTROL)
        else (data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}),
        attachments=data.get("attachments", []) if isinstance(data.get("attachments"), list) else [],
    )
