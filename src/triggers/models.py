"""
触发器数据模型

定义触发器系统的数据结构和配置模型。
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class TriggerType(str, Enum):
    """触发器类型"""

    TIME = "time"  # 时间触发器
    EVENT = "event"  # 事件触发器
    CONDITION = "condition"  # 条件触发器


class ActionType(str, Enum):
    """动作类型"""

    NOTIFICATION = "notification"  # 通知
    API_CALL = "api_call"  # API 调用
    TASK_RETRY = "task_retry"  # 任务重试
    TASK_COMPLETE = "task_complete"  # 任务完成
    CUSTOM = "custom"  # 自定义


class TriggerStatus(str, Enum):
    """触发器状态"""

    ENABLED = "enabled"
    DISABLED = "disabled"
    RUNNING = "running"
    ERROR = "error"


@dataclass
class ActionConfig:
    """动作配置"""

    type: ActionType
    config: dict[str, Any]
    order: int = 0

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {"type": self.type.value, "config": self.config, "order": self.order}


@dataclass
class TriggerConfig:
    """触发器配置"""

    id: str
    name: str
    trigger_type: TriggerType
    enabled: bool = True
    description: str | None = None
    actions: list[ActionConfig] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    # 时间触发器配置
    schedule: dict[str, Any] | None = None

    # 事件触发器配置
    event: dict[str, Any] | None = None

    # 条件触发器配置
    condition: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "id": self.id,
            "name": self.name,
            "trigger_type": self.trigger_type.value,
            "enabled": self.enabled,
            "description": self.description,
            "actions": [action.to_dict() for action in self.actions],
            "metadata": self.metadata,
            "schedule": self.schedule,
            "event": self.event,
            "condition": self.condition,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TriggerConfig":
        """从字典创建配置"""
        actions = [
            ActionConfig(
                type=ActionType(action["type"]),
                config=action["config"],
                order=action.get("order", 0),
            )
            for action in data.get("actions", [])
        ]

        return cls(
            id=data["id"],
            name=data["name"],
            trigger_type=TriggerType(data["trigger_type"]),
            enabled=data.get("enabled", True),
            description=data.get("description"),
            actions=actions,
            metadata=data.get("metadata", {}),
            schedule=data.get("schedule"),
            event=data.get("event"),
            condition=data.get("condition"),
        )


@dataclass
class ExecutionResult:
    """执行结果"""

    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    executed_at: datetime = None

    def __post_init__(self):
        if self.executed_at is None:
            self.executed_at = datetime.utcnow()

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "success": self.success,
            "message": self.message,
            "data": self.data,
            "error": self.error,
            "executed_at": self.executed_at.isoformat(),
        }
