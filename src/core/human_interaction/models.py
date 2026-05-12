"""
人类交互数据模型

定义交互请求、响应和相关枚举类型
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class InteractionType(str, Enum):
    """交互类型"""

    APPROVAL = "approval"
    CONVERSATION = "conversation"


class InteractionMode(str, Enum):
    """交互模式"""

    APPROVAL_SIMPLE = "approval_simple"
    APPROVAL_WITH_OPTIONS = "approval_with_options"
    APPROVAL_WITH_EDIT = "approval_with_edit"
    CONVERSATION_FREE = "conversation_free"
    CONVERSATION_GUIDED = "conversation_guided"


class InteractionSource(str, Enum):
    """交互来源"""

    TOOL_CALL = "tool_call"
    TASK_APPROVAL = "task_approval"
    AGENT_REQUEST = "agent_request"
    WORKFLOW = "workflow"
    SYSTEM = "system"


class InteractionStatus(str, Enum):
    """交互状态"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    AUTO_APPROVED = "auto_approved"


class ResponseType(str, Enum):
    """响应类型"""

    APPROVED = "approved"
    DENIED = "denied"
    MODIFIED = "modified"
    CONVERSATION_END = "conversation_end"
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
    RETRY = "retry"
    IGNORE = "ignore"


@dataclass
class ApprovalOption:
    """审批选项"""

    id: str
    label: str
    description: str | None = None
    style: str = "default"
    is_default: bool = False
    is_destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "style": self.style,
            "is_default": self.is_default,
            "is_destructive": self.is_destructive,
        }


@dataclass
class InteractionContext:
    """交互上下文"""

    operation: str
    risk_level: int = 5
    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "risk_level": self.risk_level,
            "data": self.data,
            "metadata": self.metadata,
        }


@dataclass
class ConversationContext:
    """对话上下文"""

    topic: str
    history: list[dict[str, Any]] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    enable_file_upload: bool = False
    enable_code_block: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "history": self.history,
            "suggestions": self.suggestions,
            "enable_file_upload": self.enable_file_upload,
            "enable_code_block": self.enable_code_block,
        }


@dataclass
class InteractionRequest:
    """
    交互请求

    统一的交互请求模型，支持审批和对话两种模式
    """

    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:12]}")
    thread_id: str = ""
    session_id: str | None = None

    interaction_type: InteractionType = InteractionType.APPROVAL
    mode: InteractionMode = InteractionMode.APPROVAL_SIMPLE

    source: InteractionSource = InteractionSource.AGENT_REQUEST
    source_id: str | None = None
    agent_id: str | None = None

    priority: Priority = Priority.NORMAL
    timeout: float = 300.0
    timeout_action: TimeoutAction = TimeoutAction.REJECT

    title: str = ""
    description: str = ""
    context: InteractionContext | None = None

    approval_options: list[ApprovalOption] = field(default_factory=list)
    default_option_id: str | None = None

    conversation_context: ConversationContext | None = None

    status: InteractionStatus = InteractionStatus.PENDING
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    expires_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "session_id": self.session_id,
            "interaction_type": self.interaction_type.value,
            "mode": self.mode.value,
            "source": self.source.value,
            "source_id": self.source_id,
            "agent_id": self.agent_id,
            "priority": self.priority.value,
            "timeout": self.timeout,
            "timeout_action": self.timeout_action.value,
            "title": self.title,
            "description": self.description,
            "context": self.context.to_dict() if self.context else None,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }

        if self.interaction_type == InteractionType.APPROVAL:
            result["approval_options"] = [opt.to_dict() for opt in self.approval_options]
            result["default_option_id"] = self.default_option_id

        if self.interaction_type == InteractionType.CONVERSATION:
            result["conversation_context"] = (
                self.conversation_context.to_dict() if self.conversation_context else None
            )

        return result

    @classmethod
    def create_approval_request(
        cls,
        thread_id: str,
        title: str,
        description: str,
        operation: str,
        risk_level: int = 5,
        options: list[ApprovalOption] | None = None,
        source: InteractionSource = InteractionSource.AGENT_REQUEST,
        source_id: str | None = None,
        agent_id: str | None = None,
        priority: Priority = Priority.NORMAL,
        timeout: float = 300.0,
        data: dict[str, Any] | None = None,
        session_id: str | None = None,
    ) -> "InteractionRequest":
        default_options = [
            ApprovalOption(id="approve", label="批准", is_default=True),
            ApprovalOption(id="deny", label="拒绝", is_destructive=True),
        ]

        mode = (
            InteractionMode.APPROVAL_WITH_OPTIONS
            if options and len(options) > 2
            else InteractionMode.APPROVAL_SIMPLE
        )

        return cls(
            thread_id=thread_id,
            session_id=session_id,
            interaction_type=InteractionType.APPROVAL,
            mode=mode,
            source=source,
            source_id=source_id,
            agent_id=agent_id,
            priority=priority,
            timeout=timeout,
            title=title,
            description=description,
            context=InteractionContext(
                operation=operation,
                risk_level=risk_level,
                data=data or {},
            ),
            approval_options=options or default_options,
            default_option_id="approve",
        )

    @classmethod
    def create_conversation_request(
        cls,
        thread_id: str,
        title: str,
        topic: str,
        description: str = "",
        source: InteractionSource = InteractionSource.AGENT_REQUEST,
        source_id: str | None = None,
        agent_id: str | None = None,
        priority: Priority = Priority.NORMAL,
        timeout: float = 600.0,
        suggestions: list[str] | None = None,
        history: list[dict[str, Any]] | None = None,
        session_id: str | None = None,
    ) -> "InteractionRequest":
        return cls(
            thread_id=thread_id,
            session_id=session_id,
            interaction_type=InteractionType.CONVERSATION,
            mode=InteractionMode.CONVERSATION_FREE,
            source=source,
            source_id=source_id,
            agent_id=agent_id,
            priority=priority,
            timeout=timeout,
            title=title,
            description=description,
            conversation_context=ConversationContext(
                topic=topic,
                history=history or [],
                suggestions=suggestions or [],
            ),
        )


@dataclass
class InteractionResponse:
    """
    交互响应

    用户对交互请求的响应
    """

    request_id: str
    response_type: ResponseType

    selected_option_id: str | None = None
    modified_data: dict[str, Any] | None = None
    reason: str | None = None

    conversation_result: str | None = None
    conversation_messages: list[dict[str, Any]] = field(default_factory=list)

    user_id: str | None = None
    responded_at: datetime = field(default_factory=datetime.now)
    duration_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "response_type": self.response_type.value,
            "selected_option_id": self.selected_option_id,
            "modified_data": self.modified_data,
            "reason": self.reason,
            "conversation_result": self.conversation_result,
            "conversation_messages": self.conversation_messages,
            "user_id": self.user_id,
            "responded_at": self.responded_at.isoformat(),
            "duration_ms": self.duration_ms,
        }

    @property
    def is_approved(self) -> bool:
        return self.response_type in (
            ResponseType.APPROVED,
            ResponseType.MODIFIED,
            ResponseType.CONVERSATION_END,
        )

    @property
    def is_denied(self) -> bool:
        return self.response_type == ResponseType.DENIED

    @classmethod
    def create_approval_response(
        cls,
        request_id: str,
        approved: bool,
        option_id: str | None = None,
        reason: str | None = None,
        modified_data: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> "InteractionResponse":
        if modified_data:
            response_type = ResponseType.MODIFIED
        elif approved:
            response_type = ResponseType.APPROVED
        else:
            response_type = ResponseType.DENIED

        return cls(
            request_id=request_id,
            response_type=response_type,
            selected_option_id=option_id,
            modified_data=modified_data,
            reason=reason,
            user_id=user_id,
        )

    @classmethod
    def create_conversation_end_response(
        cls,
        request_id: str,
        result: str,
        messages: list[dict[str, Any]],
        user_id: str | None = None,
    ) -> "InteractionResponse":
        return cls(
            request_id=request_id,
            response_type=ResponseType.CONVERSATION_END,
            conversation_result=result,
            conversation_messages=messages,
            user_id=user_id,
        )


ApprovalType = InteractionType
ApprovalStatus = InteractionStatus
ApprovalRequest = InteractionRequest
ApprovalDecision = InteractionResponse
ApprovalConfig = None  # Will be set after import to avoid circular dependency
