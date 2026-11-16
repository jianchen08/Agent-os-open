"""
人类交互数据模型

暴露接口：
- InteractionMode：InteractionMode类
- InteractionStatus：InteractionStatus类
- ResponseType：ResponseType类
- OptionSemantics：选项语义封闭枚举（单一真值源在 SDK approval_contract，此处再出口）
- Priority：Priority类
- TimeoutAction：TimeoutAction类
"""

from enum import Enum

from agentos_plugin_sdk.approval_contract import (
    OptionSemantics,  # noqa: F401 — 再出口（插件内统一从 human.models 取语义枚举）
)


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
