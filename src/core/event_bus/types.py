"""
事件总线类型定义

定义事件类型、事件数据结构和过滤器
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """事件类型枚举"""

    # 状态变更
    STATE_CHANGE = "state_change"

    # 步骤生命周期
    STEP_START = "step_start"
    STEP_COMPLETE = "step_complete"
    STEP_ERROR = "step_error"

    # 审批流程
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_RESPONSE = "approval_response"

    # 检查点
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"

    # 执行生命周期
    EXECUTION_START = "execution_start"
    EXECUTION_COMPLETE = "execution_complete"
    EXECUTION_ERROR = "execution_error"
    EXECUTION_CANCELLED = "execution_cancelled"

    # 恢复流程
    RECOVERY_START = "recovery_start"
    RECOVERY_COMPLETE = "recovery_complete"

    # Agent 相关
    AGENT_START = "agent_start"
    AGENT_COMPLETE = "agent_complete"
    AGENT_ERROR = "agent_error"
    AGENT_THINKING = "agent_thinking"

    # 工具调用
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"

    # 工具市场
    TOOL_REGISTERED = "tool_registered"
    TOOL_INSTALLED = "tool_installed"
    TOOL_UNINSTALLED = "tool_uninstalled"
    TOOL_RATED = "tool_rated"
    TOOL_UPDATED = "tool_updated"

    # 流式输出
    STREAM_START = "stream_start"
    STREAM_CHUNK = "stream_chunk"
    STREAM_END = "stream_end"

    # 消息
    NEW_MESSAGE = "new_message"

    # 系统事件
    SYSTEM_ALERT = "system_alert"
    HEARTBEAT = "heartbeat"

    # 自定义事件
    CUSTOM = "custom"

    # 任务事件（事件驱动改造）
    TASK_SUBMITTED = "task_submitted"  # 任务已提交
    TASK_EXECUTION_REQUESTED = "task_execution_requested"  # 任务执行请求
    TASK_READY_FOR_SCHEDULING = "task_ready_for_scheduling"  # 任务已准备好调度
    TASK_TIMEOUT = "task_timeout"  # 任务超时
    TASK_CANCELLED = "task_cancelled"  # 任务已取消
    TASK_STATUS_CHANGED = "task_status_changed"  # 任务状态变更


class EventPriority(int, Enum):
    """事件优先级"""

    LOW = 0
    NORMAL = 1
    HIGH = 2
    CRITICAL = 3


class ExecutionEvent(BaseModel):
    """执行事件数据模型"""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12], description="事件唯一 ID")
    event_type: EventType = Field(..., description="事件类型")
    session_id: str = Field(..., description="会话/执行 ID")
    data: dict[str, Any] = Field(default_factory=dict, description="事件数据")
    timestamp: datetime = Field(default_factory=datetime.now, description="事件时间戳")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    priority: EventPriority = Field(default=EventPriority.NORMAL, description="事件优先级")
    source: str | None = Field(None, description="事件来源（进程/实例标识）")

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
            "priority": self.priority.value,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExecutionEvent":
        """从字典反序列化"""
        timestamp = data.get("timestamp")
        timestamp = datetime.fromisoformat(timestamp) if isinstance(timestamp, str) else datetime.now()

        return cls(
            event_id=data.get("event_id", uuid.uuid4().hex[:12]),
            event_type=EventType(data["event_type"]),
            session_id=data["session_id"],
            data=data.get("data", {}),
            timestamp=timestamp,
            metadata=data.get("metadata", {}),
            priority=EventPriority(data.get("priority", EventPriority.NORMAL.value)),
            source=data.get("source"),
        )

    def to_stream_data(self) -> dict[str, str]:
        """转换为 Redis Stream 数据格式（所有值必须是字符串）"""
        import json  # noqa: PLC0415

        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "session_id": self.session_id,
            "data": json.dumps(self.data, ensure_ascii=False, default=str),
            "timestamp": self.timestamp.isoformat(),
            "metadata": json.dumps(self.metadata, ensure_ascii=False, default=str),
            "priority": str(self.priority.value),
            "source": self.source or "",
        }

    @classmethod
    def from_stream_data(cls, data: dict[str, str]) -> "ExecutionEvent":
        """从 Redis Stream 数据格式反序列化"""
        import json  # noqa: PLC0415

        return cls(
            event_id=data.get("event_id", uuid.uuid4().hex[:12]),
            event_type=EventType(data["event_type"]),
            session_id=data["session_id"],
            data=json.loads(data.get("data", "{}")),
            timestamp=datetime.fromisoformat(data["timestamp"]),
            metadata=json.loads(data.get("metadata", "{}")),
            priority=EventPriority(int(data.get("priority", "1"))),
            source=data.get("source") or None,
        )


@dataclass
class EventFilter:
    """事件过滤器"""

    event_types: list[EventType] | None = None
    session_ids: list[str] | None = None
    min_priority: EventPriority | None = None
    sources: list[str] | None = None
    custom_event_types: list[str] | None = None

    def matches(self, event: ExecutionEvent) -> bool:  # noqa: PLR0911
        """检查事件是否匹配过滤器"""
        if self.event_types and event.event_type not in self.event_types:
            _logger.debug(
                "[EventFilter] 匹配失败 | reason=event_types | event_type=%s | required=%s",
                event.event_type.value,
                [et.value for et in self.event_types],
            )
            return False

        if self.custom_event_types:
            if event.event_type != EventType.CUSTOM:
                _logger.debug(
                    "[EventFilter] 匹配失败 | reason=not_custom | event_type=%s",
                    event.event_type.value,
                )
                return False
            custom_type = event.data.get("custom_event_type", "")
            if custom_type not in self.custom_event_types:
                _logger.debug(
                    "[EventFilter] 匹配失败 | reason=custom_type_mismatch | custom_type=%s | required=%s",
                    custom_type,
                    self.custom_event_types,
                )
                return False

        if self.session_ids and event.session_id not in self.session_ids:
            _logger.debug(
                "[EventFilter] 匹配失败 | reason=session_id | session_id=%s | required=%s",
                event.session_id,
                self.session_ids,
            )
            return False

        if self.min_priority and event.priority.value < self.min_priority.value:
            _logger.debug(
                "[EventFilter] 匹配失败 | reason=priority | event_priority=%s | min_priority=%s",
                event.priority.value,
                self.min_priority.value,
            )
            return False

        if self.sources and event.source not in self.sources:
            _logger.debug(
                "[EventFilter] 匹配失败 | reason=source | source=%s | required=%s",
                event.source,
                self.sources,
            )
            return False

        return True


# 事件处理器类型
EventHandler = Callable[[ExecutionEvent], Awaitable[None]]


@dataclass
class Subscription:
    """订阅信息"""

    id: str
    handler: EventHandler
    filter: EventFilter | None = None
    consumer_group: str | None = None
