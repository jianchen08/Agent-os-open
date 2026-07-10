"""
人类交互数据模型

暴露接口：
- InteractionMode：InteractionMode类
- InteractionStatus：InteractionStatus类
- ResponseType：ResponseType类
- Priority：Priority类
- TimeoutAction：TimeoutAction类
"""

from enum import Enum


class InteractionMode(str, Enum):
    """交互模式"""

    CHOICE = "choice"
    CONVERSATION = "conversation"
    NOTIFICATION = "notification"


class InteractionStatus(str, Enum):
    """交互状态"""

    PENDING = "pending"
    VIEWED = "viewed"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    AUTO_APPROVED = "auto_approved"


class ResponseType(str, Enum):
    """响应类型"""

    APPROVED = "approved"
    DENIED = "denied"
    ANSWERED = "answered"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class Priority(str, Enum):
    """优先级"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class TimeoutAction(str, Enum):
    """超时处理策略"""

    REJECT = "reject"
    AUTO_APPROVE = "auto_approve"
    IGNORE = "ignore"
