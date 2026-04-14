"""
统一人类交互模块

提供统一的人类交互抽象层，支持两种交互模式：
1. 选择模式 - 弹出选择框，等待用户做出决定
2. 对话模式 - 跳转到对话标签页

所有需要人类参与的场景都通过此模块统一处理。
"""

from src.core.human_interaction.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from src.core.human_interaction.models import (
    InteractionMode,
    InteractionStatus,
    Priority,
    ResponseType,
    TimeoutAction,
)
from src.core.human_interaction.service import (
    HumanInteractionService,
    InteractionCancelledError,
    InteractionDeniedError,
    InteractionTimeoutError,
    get_human_interaction_service,
    reset_human_interaction_service,
    set_human_interaction_service,
)

__all__ = [
    "IHumanInteractionService",
    "IInteractionNotifier",
    "HumanInteractionService",
    "get_human_interaction_service",
    "set_human_interaction_service",
    "reset_human_interaction_service",
    "InteractionMode",
    "InteractionStatus",
    "ResponseType",
    "Priority",
    "TimeoutAction",
    "InteractionTimeoutError",
    "InteractionCancelledError",
    "InteractionDeniedError",
]
