"""
WebSocket 模块

事件系统整合说明：
- 统一执行事件：execution_start/progress/done/cancelled
- 思考模式事件：thinking_start/chunk/end
- 交互事件：interaction_requested（统一审批/对话）
- 系统警告：cost_warning/resource_limit
"""

from src.api.websocket.error_codes import (
    ERROR_MESSAGES,
    WebSocketErrorCode,
    get_error_message,
    is_retryable_error,
)
from src.api.websocket.events import (
    AuthMessage,
    AutoExecuteTriggeredEvent,
    ClarificationNeededEvent,
    CostWarningEvent,
    DoneEvent,
    ErrorEvent,
    EvaluationResultEvent,
    ExecutionCancelledEvent,
    ExecutionDoneEvent,
    ExecutionOutputEvent,
    ExecutionProgressEvent,
    ExecutionStartEvent,
    InteractionRequestedEvent,
    InterruptEvent,
    PlanUpdateEvent,
    ResourceLimitEvent,
    ResumeActionMessage,
    StateChangeEvent,
    StopGenerationMessage,
    TaskACEvaluatedEvent,
    TaskCompletedEvent,
    TaskCreatedEvent,
    TaskFailedEvent,
    TaskPhaseChangedEvent,
    ThinkingChunkEvent,
    ThinkingEndEvent,
    ThinkingStartEvent,
    TokenStreamEvent,
    UserInputMessage,
    create_auto_execute_triggered_event,
    create_clarification_needed_event,
    create_cost_warning_event,
    create_done_event,
    create_error_event,
    create_execution_cancelled_event,
    create_execution_done_event,
    create_execution_output_event,
    create_execution_progress_event,
    create_execution_start_event,
    create_interaction_requested_event,
    create_resource_limit_event,
    create_state_change_event,
    create_task_ac_evaluated_event,
    create_task_completed_event,
    create_task_created_event,
    create_task_failed_event,
    create_task_phase_changed_event,
    create_thinking_chunk_event,
    create_thinking_end_event,
    create_thinking_start_event,
    create_token_stream_event,
)
from src.api.websocket.handler import ConnectionManager

__all__ = [
    # ========== 服务端事件 ==========
    # 基础事件
    "TokenStreamEvent",
    "StateChangeEvent",
    "PlanUpdateEvent",
    "EvaluationResultEvent",
    "InterruptEvent",
    "ErrorEvent",
    "DoneEvent",
    # 任务执行闭环事件
    "TaskCreatedEvent",
    "TaskPhaseChangedEvent",
    "TaskACEvaluatedEvent",
    "TaskCompletedEvent",
    "TaskFailedEvent",
    # 用户交互事件
    "ClarificationNeededEvent",
    "InteractionRequestedEvent",
    # 统一执行卡片事件
    "ExecutionStartEvent",
    "ExecutionProgressEvent",
    "ExecutionDoneEvent",
    "ExecutionCancelledEvent",
    "ExecutionOutputEvent",
    # 思考模式事件
    "ThinkingStartEvent",
    "ThinkingChunkEvent",
    "ThinkingEndEvent",
    # 系统警告事件
    "CostWarningEvent",
    "ResourceLimitEvent",
    # 自动执行触发事件（已废弃）
    "AutoExecuteTriggeredEvent",
    # ========== 客户端消息 ==========
    "UserInputMessage",
    "ResumeActionMessage",
    "StopGenerationMessage",
    "AuthMessage",
    # ========== 工厂函数 ==========
    # 基础工厂函数
    "create_token_stream_event",
    "create_state_change_event",
    "create_error_event",
    "create_done_event",
    # 任务执行闭环事件工厂函数
    "create_task_created_event",
    "create_task_phase_changed_event",
    "create_task_ac_evaluated_event",
    "create_task_completed_event",
    "create_task_failed_event",
    # 用户交互事件工厂函数
    "create_clarification_needed_event",
    "create_interaction_requested_event",
    # 统一执行卡片事件工厂函数
    "create_execution_start_event",
    "create_execution_progress_event",
    "create_execution_done_event",
    "create_execution_cancelled_event",
    "create_execution_output_event",
    # 思考模式事件工厂函数
    "create_thinking_start_event",
    "create_thinking_chunk_event",
    "create_thinking_end_event",
    # 系统警告事件工厂函数
    "create_cost_warning_event",
    "create_resource_limit_event",
    # 自动执行触发事件工厂函数（已废弃）
    "create_auto_execute_triggered_event",
    # ========== 连接管理 ==========
    "ConnectionManager",
    # ========== 错误码 ==========
    "WebSocketErrorCode",
    "ERROR_MESSAGES",
    "get_error_message",
    "is_retryable_error",
]
