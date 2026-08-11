"""
状态变更事件定义

暴露接口：
- to_dict(self) -> dict[str, Any]：to_dict功能
- StateEventType：StateEventType类
- StateEvent：StateEvent类
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class StateEventType(str, Enum):
    """
    状态事件类型

    定义状态变更事件的类型。

    类型说明:
        - TRANSITION: 正常状态转换
        - ROLLBACK: 状态回滚
        - TIMEOUT: 状态超时
        - ERROR: 状态错误
    """

    TRANSITION = "transition"
    ROLLBACK = "rollback"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class StateEvent:
    """
    状态变更事件

    记录状态变更的详细信息，包括实体信息、状态变化、原因等。

    Attributes:
        entity_type: 实体类型（task/agent/workflow/trigger 等）
        entity_id: 实体唯一标识
        from_state: 源状态值
        to_state: 目标状态值
        event_type: 事件类型，默认为 TRANSITION
        reason: 状态变更原因，可选
        metadata: 附加元数据，可选
        timestamp: 事件时间戳，默认为当前 UTC 时间

    Example:
        >>> event = StateEvent(
        ...     entity_type="task",
        ...     entity_id="task-123",
        ...     from_state="pending",
        ...     to_state="running",
        ...     reason="开始执行任务",
        ... )
        >>> event.to_dict()
        {'entity_type': 'task', 'entity_id': 'task-123', ...}
    """

    entity_type: str
    entity_id: str
    from_state: str
    to_state: str
    event_type: StateEventType = StateEventType.TRANSITION
    reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        """转换为字典格式"""
        return {
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "event_type": self.event_type.value,
            "reason": self.reason,
            "metadata": self.metadata,
            "timestamp": self.timestamp.isoformat(),
        }
