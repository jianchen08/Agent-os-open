"""
统一人类交互模块

提供统一的人类交互抽象层，支持两种交互模式：
1. 审批模式 - 传统的审批流程
2. 对话模式 - 直接与 Agent 对话

所有需要人类参与的场景都通过此模块统一处理。
"""

from src.core.human_interaction.interfaces import (
    IHumanInteractionService,
    IInteractionNotifier,
)
from src.core.human_interaction.models import (
    ApprovalConfig,
    ApprovalDecision,
    ApprovalOption,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalType,
    ConversationContext,
    InteractionContext,
    InteractionMode,
    InteractionRequest,
    InteractionResponse,
    InteractionSource,
    InteractionStatus,
    InteractionType,
    Priority,
    ResponseType,
    TimeoutAction,
)
from src.core.human_interaction.service import (
    HumanInteractionService,
    get_human_interaction_service,
    reset_human_interaction_service,
)

__all__ = [
    "IHumanInteractionService",
    "IInteractionNotifier",
    "HumanInteractionService",
    "get_human_interaction_service",
    "reset_human_interaction_service",
    "InteractionRequest",
    "InteractionResponse",
    "InteractionContext",
    "ConversationContext",
    "ApprovalOption",
    "InteractionType",
    "InteractionMode",
    "InteractionSource",
    "InteractionStatus",
    "ResponseType",
    "Priority",
    "TimeoutAction",
    "ApprovalType",
    "ApprovalStatus",
    "ApprovalRequest",
    "ApprovalDecision",
    "ApprovalConfig",
]
