"""统一消息格式系统。

定义全局共用的消息类型枚举、统一消息模型和格式化工具函数，
确保 WebSocket 推送消息和 HTTP API 响应使用完全相同的消息结构。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class MessageType(str, Enum):
    """消息类型枚举。"""

    THINKING = "thinking"
    EXECUTING = "executing"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessageSubtype(str, Enum):
    """消息子类型枚举。"""

    TEXT = "text"
    ERROR = "error"
    PROGRESS = "progress"
    STATUS = "status"
    SYSTEM = "system"


MESSAGE_TYPE_UI_MAP: dict[MessageType, dict[str, str]] = {
    MessageType.THINKING: {"color": "#6366f1", "icon": "brain", "label": "思考中"},
    MessageType.EXECUTING: {"color": "#3b82f6", "icon": "play", "label": "执行中"},
    MessageType.WAITING: {"color": "#f59e0b", "icon": "clock", "label": "等待中"},
    MessageType.COMPLETED: {"color": "#22c55e", "icon": "check-circle", "label": "已完成"},
    MessageType.FAILED: {"color": "#ef4444", "icon": "x-circle", "label": "失败"},
    MessageType.CANCELLED: {"color": "#9ca3af", "icon": "ban", "label": "已取消"},
}


class UnifiedMessage(BaseModel):
    """统一消息模型。"""

    type: MessageType = Field(..., description="消息类型")
    subtype: MessageSubtype | None = Field(None, description="消息子类型")
    status: str = Field("", description="状态")
    content: dict[str, Any] = Field(default_factory=dict, description="消息内容")
    timestamp: str = Field("", description="ISO 8601 时间戳（带时区）")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")

    @model_validator(mode="after")
    def _set_defaults(self) -> UnifiedMessage:
        """设置默认值：status 跟随 type，timestamp 自动生成。"""
        if not self.status:
            self.status = self.type.value
        if not self.timestamp:
            self.timestamp = format_timestamp()
        return self

    def to_dict(self) -> dict[str, Any]:
        """将消息序列化为字典。"""
        return {
            "type": self.type.value,
            "subtype": self.subtype.value if self.subtype else None,
            "status": self.status,
            "content": self.content,
            "timestamp": self.timestamp,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UnifiedMessage:
        """从字典反序列化为 UnifiedMessage。"""
        return cls(
            type=data["type"],
            subtype=data.get("subtype"),
            status=data.get("status", ""),
            content=data.get("content", {}),
            timestamp=data.get("timestamp", ""),
            metadata=data.get("metadata", {}),
        )


def format_timestamp(dt: datetime | None = None) -> str:
    """格式化时间为 ISO 8601 字符串（带时区）。"""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.isoformat()


def validate_message_dict(data: dict[str, Any]) -> bool:
    """验证消息字典是否符合 UnifiedMessage 格式。"""
    if "type" not in data:
        return False
    try:
        MessageType(data["type"])
    except (ValueError, KeyError):
        return False
    return True


def create_message(
    msg_type: MessageType,
    content: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    subtype: MessageSubtype | None = None,
    status: str = "",
) -> UnifiedMessage:
    """创建统一消息的通用工厂函数。"""
    return UnifiedMessage(
        type=msg_type,
        subtype=subtype,
        status=status,
        content=content or {},
        metadata=metadata or {},
    )


def create_thinking_message(
    text: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建思考中消息。"""
    content: dict[str, Any] = {"text": text, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(MessageType.THINKING, content=content, metadata=metadata)


def create_executing_message(
    tool_name: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建执行中消息。"""
    content: dict[str, Any] = {"tool_name": tool_name, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(MessageType.EXECUTING, content=content, metadata=metadata)


def create_waiting_message(
    reason: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建等待中消息。"""
    content: dict[str, Any] = {"reason": reason, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(MessageType.WAITING, content=content, metadata=metadata)


def create_completed_message(
    result: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建完成消息。"""
    content: dict[str, Any] = {"result": result, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(MessageType.COMPLETED, content=content, metadata=metadata)


def create_failed_message(
    error: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建失败消息。"""
    content: dict[str, Any] = {"error": error, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(
        MessageType.FAILED, content=content, metadata=metadata,
        subtype=MessageSubtype.ERROR,
    )


def create_cancelled_message(
    reason: str = "", *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建已取消消息。"""
    content: dict[str, Any] = {"reason": reason, **extra_content}
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(MessageType.CANCELLED, content=content, metadata=metadata)


def create_progress_message(
    progress: float = 0.0, description: str = "",
    *, task_id: str = "", agent_id: str = "", session_id: str = "",
    **extra_content: Any,
) -> UnifiedMessage:
    """创建进度消息。"""
    content: dict[str, Any] = {
        "progress": progress, "description": description, **extra_content,
    }
    metadata = _build_metadata(task_id, agent_id, session_id)
    return create_message(
        MessageType.EXECUTING, content=content, metadata=metadata,
        subtype=MessageSubtype.PROGRESS,
    )


def _build_metadata(
    task_id: str = "", agent_id: str = "", session_id: str = "",
) -> dict[str, str]:
    """构建元数据字典，仅包含非空字段。"""
    metadata: dict[str, str] = {}
    if task_id:
        metadata["task_id"] = task_id
    if agent_id:
        metadata["agent_id"] = agent_id
    if session_id:
        metadata["session_id"] = session_id
    return metadata
