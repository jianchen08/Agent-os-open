"""
WebSocket 类型定义导出端点

提供消息类型定义的 API 端点，用于前端生成类型定义
"""

from fastapi import APIRouter

from src.api.websocket.message_types import (
    MessageType,
    MessageTypes,
)

# 临时方案：MESSAGE_TYPE_SCHEMAS 未实现
MESSAGE_TYPE_SCHEMAS = {}

router = APIRouter(prefix="/ws-types", tags=["websocket-types"])


@router.get("/message-types")
async def get_message_types():
    """
    获取所有消息类型定义

    返回所有 WebSocket 消息类型的定义，用于前端生成类型定义
    """
    return {
        "message_types": {
            name: value
            for name, value in MessageTypes.__dict__.items()
            if not name.startswith("_")
        },
        "message_type_schemas": MESSAGE_TYPE_SCHEMAS,
    }


@router.get("/message-types/enums")
async def get_message_type_enums():
    """
    获取消息类型枚举值

    返回 MessageType 枚举的所有值
    """
    return {
        "message_types": [msg_type.value for msg_type in MessageType],
        "enums": {msg_type.name: msg_type.value for msg_type in MessageType},
    }
