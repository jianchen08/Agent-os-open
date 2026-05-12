"""
WebSocket 事件定义

定义服务端和客户端之间的 WebSocket 消息格式
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ============================================================================
# 服务端 -> 客户端事件
# ============================================================================


class BaseServerEvent(BaseModel):
    """服务端事件基类"""

    type: str = Field(..., description="事件类型")
    timestamp: datetime = Field(default_factory=datetime.now, description="时间戳")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return self.model_dump(mode="json")


# ============================================================================
# 任务执行闭环相关事件
# ============================================================================


class TaskCreatedEvent(BaseServerEvent):
    """短期任务创建事件"""

    type: Literal["task_created"] = "task_created"
    taskId: str = Field(..., description="任务 ID")
    goal: str = Field(..., description="任务目标")
    taskType: str = Field(..., description="任务类型")
    phase: str = Field(..., description="当前阶段")


class TaskPhaseChangedEvent(BaseServerEvent):
    """任务阶段变更事件"""

    type: Literal["task_phase_changed"] = "task_phase_changed"
    taskId: str = Field(..., description="任务 ID")
    phase: str = Field(..., description="新阶段")
    status: str = Field(..., description="阶段状态")
    timestamp: datetime = Field(..., description="变更时间")


class TaskACEvaluatedEvent(BaseServerEvent):
    """AC 评估完成事件"""

    type: Literal["task_ac_evaluated"] = "task_ac_evaluated"
    taskId: str = Field(..., description="任务 ID")
    acId: str = Field(..., description="验收标准 ID")
    passed: bool = Field(..., description="是否通过")
    result: dict[str, Any] = Field(..., description="评估结果")


class TaskCompletedEvent(BaseServerEvent):
    """任务完成事件"""

    type: Literal["task_completed"] = "task_completed"
    taskId: str = Field(..., description="任务 ID")
    result: dict[str, Any] = Field(..., description="执行结果")
    summary: str = Field(..., description="执行总结")


class TaskFailedEvent(BaseServerEvent):
    """任务失败事件"""

    type: Literal["task_failed"] = "task_failed"
    taskId: str = Field(..., description="任务 ID")
    error: str = Field(..., description="错误信息")
    retryCount: int = Field(..., description="重试次数")


# ============================================================================
# 用户交互相关事件
# ============================================================================


class ClarificationNeededEvent(BaseServerEvent):
    """澄清请求事件 - 通知用户需要补充信息"""

    type: Literal["clarification_needed"] = "clarification_needed"
    taskId: str = Field(..., description="任务 ID")
    sessionId: str = Field(..., description="会话 ID")
    tabId: str = Field(..., description="Agent Tab ID")
    questions: list = Field(..., description="需要澄清的问题列表")
    context: str | None = Field(None, description="澄清上下文说明")


class InteractionRequestedEvent(BaseServerEvent):
    """交互请求事件 - 统一的人类交互场景（审批/对话）"""

    type: Literal["interaction_requested"] = "interaction_requested"
    requestId: str = Field(..., description="交互请求 ID")
    interactionType: str = Field(..., description="交互类型: approval/conversation")
    source: str = Field(..., description="来源: agent/tool/workflow")
    sourceId: str = Field(..., description="来源 ID")
    title: str = Field(..., description="交互标题")
    description: str = Field(..., description="交互描述")
    context: dict[str, Any] | None = Field(None, description="上下文信息")
    options: dict[str, Any] | None = Field(None, description="可选操作")
    timeout: int | None = Field(None, description="超时时间（秒）")
    priority: str = Field(default="normal", description="优先级: low/normal/high/urgent")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


# ============================================================================
# 统一执行卡片事件（工具/Agent/工作流）
# ============================================================================


class ExecutionStartEvent(BaseServerEvent):
    """执行开始事件 - 统一工具/Agent/工作流"""

    type: Literal["execution_start"] = "execution_start"
    executionId: str = Field(..., description="执行 ID")
    executionType: str = Field(..., description="执行类型: tool/agent/workflow")
    name: str = Field(..., description="名称")
    description: str | None = Field(None, description="描述")
    parentId: str | None = Field(None, description="父执行 ID（嵌套时使用）")
    input: dict[str, Any] | None = Field(None, description="输入参数")
    metadata: dict[str, Any] | None = Field(None, description="元数据")


class ExecutionProgressEvent(BaseServerEvent):
    """执行进度事件"""

    type: Literal["execution_progress"] = "execution_progress"
    executionId: str = Field(..., description="执行 ID")
    progress: int = Field(..., description="进度百分比 0-100")
    currentStep: str | None = Field(None, description="当前步骤描述")
    message: str | None = Field(None, description="进度消息")


class ExecutionDoneEvent(BaseServerEvent):
    """执行完成事件"""

    type: Literal["execution_done"] = "execution_done"
    executionId: str = Field(..., description="执行 ID")
    success: bool = Field(..., description="是否成功")
    output: dict[str, Any] | None = Field(None, description="输出结果")
    error: str | None = Field(None, description="错误信息")
    durationMs: int | None = Field(None, description="耗时（毫秒）")
    summary: str | None = Field(None, description="执行摘要")


class ExecutionCancelledEvent(BaseServerEvent):
    """执行取消事件"""

    type: Literal["execution_cancelled"] = "execution_cancelled"
    executionId: str = Field(..., description="执行 ID")
    reason: str = Field(..., description="取消原因")
    cancelledBy: str | None = Field(None, description="取消者: user/system/timeout")


class ExecutionOutputEvent(BaseServerEvent):
    """执行输出事件（中间输出）"""

    type: Literal["execution_output"] = "execution_output"
    executionId: str = Field(..., description="执行 ID")
    output: str = Field(..., description="新增的输出内容")
    append: bool = Field(default=True, description="true=追加, false=替换")


# ============================================================================
# 思考模式事件
# ============================================================================


class ThinkingStartEvent(BaseServerEvent):
    """思考开始事件"""

    type: Literal["thinking_start"] = "thinking_start"
    executionId: str = Field(..., description="执行 ID")
    model: str | None = Field(None, description="模型名称")


class ThinkingChunkEvent(BaseServerEvent):
    """思考内容片段事件"""

    type: Literal["thinking_chunk"] = "thinking_chunk"
    executionId: str = Field(..., description="执行 ID")
    chunk: str = Field(..., description="思考内容片段")


class ThinkingEndEvent(BaseServerEvent):
    """思考结束事件"""

    type: Literal["thinking_end"] = "thinking_end"
    executionId: str = Field(..., description="执行 ID")
    durationMs: int | None = Field(None, description="思考耗时（毫秒）")


# ============================================================================
# 系统警告事件
# ============================================================================


class CostWarningEvent(BaseServerEvent):
    """成本预警事件"""

    type: Literal["cost_warning"] = "cost_warning"
    executionId: str = Field(..., description="执行 ID")
    currentCost: float = Field(..., description="当前成本")
    threshold: float = Field(..., description="阈值")
    message: str = Field(..., description="警告消息")


class ResourceLimitEvent(BaseServerEvent):
    """资源限制事件"""

    type: Literal["resource_limit"] = "resource_limit"
    executionId: str = Field(..., description="执行 ID")
    limitType: str = Field(..., description="限制类型: iterations/time/tokens")
    current: int = Field(..., description="当前值")
    limit: int = Field(..., description="限制值")
    message: str = Field(..., description="警告消息")


class HeartbeatAckEvent(BaseServerEvent):
    """心跳确认事件"""

    type: Literal["heartbeat_ack"] = "heartbeat_ack"


class ConnectionEstablishedEvent(BaseServerEvent):
    """连接建立事件"""

    type: Literal["connection_established"] = "connection_established"
    thread_id: str = Field(..., description="线程 ID")
    user_id: str = Field(..., description="用户 ID")


class StreamStartEvent(BaseServerEvent):
    """流开始事件"""

    type: Literal["stream_start"] = "stream_start"
    node: str = Field(..., description="节点名称")


class StreamChunkEvent(BaseServerEvent):
    """流数据块事件"""

    type: Literal["stream_chunk"] = "stream_chunk"
    chunk: str = Field(..., description="数据块")
    node: str = Field(..., description="节点名称")


class StreamEndEvent(BaseServerEvent):
    """流结束事件"""

    type: Literal["stream_end"] = "stream_end"
    node: str = Field(..., description="节点名称")


class TokenStreamEvent(BaseServerEvent):
    """Token 流事件"""

    type: Literal["token_stream"] = "token_stream"
    chunk: str = Field(..., description="Token 片段")
    node: str = Field(..., description="产生 Token 的节点名称")


class StateChangeEvent(BaseServerEvent):
    """状态变更事件"""

    type: Literal["state_change"] = "state_change"
    state: str = Field(..., description="新状态")
    previous_state: str | None = Field(None, description="前一个状态")


class PlanUpdateEvent(BaseServerEvent):
    """计划更新事件"""

    type: Literal["plan_update"] = "plan_update"
    plan: dict[str, Any] = Field(..., description="执行计划")


class EvaluationResultEvent(BaseServerEvent):
    """评估结果事件"""

    type: Literal["evaluation_result"] = "evaluation_result"
    report: dict[str, Any] = Field(..., description="评估报告")


class InterruptEvent(BaseServerEvent):
    """中断事件（需要用户决策）"""

    type: Literal["interrupt"] = "interrupt"
    reason: str = Field(..., description="中断原因")
    current_state: dict[str, Any] = Field(..., description="当前状态")
    options: dict[str, Any] | None = Field(None, description="可选操作")


class ErrorEvent(BaseServerEvent):
    """错误事件"""

    type: Literal["error"] = "error"
    code: str = Field(..., description="错误码")
    message: str = Field(..., description="错误消息")
    detail: str | None = Field(None, description="详细信息")


class DoneEvent(BaseServerEvent):
    """完成事件"""

    type: Literal["done"] = "done"
    result: dict[str, Any] | None = Field(None, description="最终结果")


# ============================================================================
# 客户端 -> 服务端消息
# ============================================================================


class BaseClientMessage(BaseModel):
    """客户端消息基类"""

    type: str = Field(..., description="消息类型")


class UserInputMessage(BaseClientMessage):
    """用户输入消息"""

    type: Literal["user_input"] = "user_input"
    content: str = Field(..., description="用户输入内容")
    context: dict[str, Any] | None = Field(None, description="额外上下文")


class ResumeActionMessage(BaseClientMessage):
    """恢复操作消息（HITL 场景）"""

    type: Literal["resume_action"] = "resume_action"
    action: Literal["approve", "reject", "modify"] = Field(..., description="操作类型")
    state_patch: dict[str, Any] | None = Field(None, description="状态修改")


class StopGenerationMessage(BaseClientMessage):
    """停止生成消息"""

    type: Literal["stop_generation"] = "stop_generation"


class AuthMessage(BaseClientMessage):
    """认证消息"""

    type: Literal["auth"] = "auth"
    token: str = Field(..., description="JWT Token")


# ============================================================================
# 事件工厂函数
# ============================================================================


def create_token_stream_event(chunk: str, node: str) -> TokenStreamEvent:
    """创建 Token 流事件"""
    return TokenStreamEvent(chunk=chunk, node=node)


def create_state_change_event(
    state: str, previous_state: str | None = None
) -> StateChangeEvent:
    """创建状态变更事件"""
    return StateChangeEvent(state=state, previous_state=previous_state)


def create_error_event(
    code: str, message: str, detail: str | None = None
) -> ErrorEvent:
    """创建错误事件"""
    return ErrorEvent(code=code, message=message, detail=detail)


def create_done_event(result: dict[str, Any] | None = None) -> DoneEvent:
    """创建完成事件"""
    return DoneEvent(result=result)


# ============================================================================
# 任务执行闭环事件工厂函数
# ============================================================================


def create_task_created_event(
    taskId: str, goal: str, taskType: str, phase: str
) -> TaskCreatedEvent:
    """创建短期任务创建事件"""
    return TaskCreatedEvent(
        taskId=taskId, goal=goal, taskType=taskType, phase=phase
    )


def create_task_phase_changed_event(
    taskId: str, phase: str, status: str, timestamp: datetime
) -> TaskPhaseChangedEvent:
    """创建任务阶段变更事件"""
    return TaskPhaseChangedEvent(
        taskId=taskId, phase=phase, status=status, timestamp=timestamp
    )


def create_task_ac_evaluated_event(
    taskId: str, acId: str, passed: bool, result: dict[str, Any]
) -> TaskACEvaluatedEvent:
    """创建 AC 评估完成事件"""
    return TaskACEvaluatedEvent(taskId=taskId, acId=acId, passed=passed, result=result)


def create_task_completed_event(
    taskId: str, result: dict[str, Any], summary: str
) -> TaskCompletedEvent:
    """创建任务完成事件"""
    return TaskCompletedEvent(taskId=taskId, result=result, summary=summary)


def create_task_failed_event(
    taskId: str, error: str, retryCount: int
) -> TaskFailedEvent:
    """创建任务失败事件"""
    return TaskFailedEvent(taskId=taskId, error=error, retryCount=retryCount)


# ============================================================================
# 用户交互事件工厂函数
# ============================================================================


def create_clarification_needed_event(
    taskId: str,
    sessionId: str,
    tabId: str,
    questions: list,
    context: str | None = None,
) -> "ClarificationNeededEvent":
    """创建澄清请求事件"""
    return ClarificationNeededEvent(
        taskId=taskId,
        sessionId=sessionId,
        tabId=tabId,
        questions=questions,
        context=context,
    )


def create_interaction_requested_event(
    requestId: str,
    interactionType: str,
    source: str,
    sourceId: str,
    title: str,
    description: str,
    context: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    timeout: int | None = None,
    priority: str = "normal",
    metadata: dict[str, Any] | None = None,
) -> "InteractionRequestedEvent":
    """创建交互请求事件"""
    return InteractionRequestedEvent(
        requestId=requestId,
        interactionType=interactionType,
        source=source,
        sourceId=sourceId,
        title=title,
        description=description,
        context=context,
        options=options,
        timeout=timeout,
        priority=priority,
        metadata=metadata,
    )


# ============================================================================
# 统一执行卡片事件工厂函数
# ============================================================================


def create_execution_start_event(
    executionId: str,
    executionType: str,
    name: str,
    description: str | None = None,
    parentId: str | None = None,
    input: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> "ExecutionStartEvent":
    """创建执行开始事件"""
    return ExecutionStartEvent(
        executionId=executionId,
        executionType=executionType,
        name=name,
        description=description,
        parentId=parentId,
        input=input,
        metadata=metadata,
    )


def create_execution_progress_event(
    executionId: str,
    progress: int,
    currentStep: str | None = None,
    message: str | None = None,
) -> "ExecutionProgressEvent":
    """创建执行进度事件"""
    return ExecutionProgressEvent(
        executionId=executionId,
        progress=progress,
        currentStep=currentStep,
        message=message,
    )


def create_execution_done_event(
    executionId: str,
    success: bool,
    output: dict[str, Any] | None = None,
    error: str | None = None,
    durationMs: int | None = None,
    summary: str | None = None,
) -> "ExecutionDoneEvent":
    """创建执行完成事件"""
    return ExecutionDoneEvent(
        executionId=executionId,
        success=success,
        output=output,
        error=error,
        durationMs=durationMs,
        summary=summary,
    )


def create_execution_cancelled_event(
    executionId: str,
    reason: str,
    cancelledBy: str | None = None,
) -> "ExecutionCancelledEvent":
    """创建执行取消事件"""
    return ExecutionCancelledEvent(
        executionId=executionId,
        reason=reason,
        cancelledBy=cancelledBy,
    )


def create_execution_output_event(
    executionId: str,
    output: str,
    append: bool = True,
) -> "ExecutionOutputEvent":
    """创建执行输出事件"""
    return ExecutionOutputEvent(
        executionId=executionId,
        output=output,
        append=append,
    )


# ============================================================================
# 思考模式事件工厂函数
# ============================================================================


def create_thinking_start_event(
    executionId: str,
    model: str | None = None,
) -> "ThinkingStartEvent":
    """创建思考开始事件"""
    return ThinkingStartEvent(
        executionId=executionId,
        model=model,
    )


def create_thinking_chunk_event(
    executionId: str,
    chunk: str,
) -> "ThinkingChunkEvent":
    """创建思考内容片段事件"""
    return ThinkingChunkEvent(
        executionId=executionId,
        chunk=chunk,
    )


def create_thinking_end_event(
    executionId: str,
    durationMs: int | None = None,
) -> "ThinkingEndEvent":
    """创建思考结束事件"""
    return ThinkingEndEvent(
        executionId=executionId,
        durationMs=durationMs,
    )


# ============================================================================
# 自动执行触发事件（已废弃，保留用于向后兼容）
# ============================================================================


class AutoExecuteTriggeredEvent(BaseServerEvent):
    """自动执行触发事件 - 当任务被自动执行时触发 [已废弃]"""

    type: Literal["auto_execute_triggered"] = "auto_execute_triggered"
    projectId: str = Field(..., description="项目 ID")
    taskId: str = Field(..., description="任务 ID")
    timestamp: datetime = Field(..., description="触发时间")


# ============================================================================
# 系统警告事件工厂函数
# ============================================================================


def create_cost_warning_event(
    executionId: str,
    currentCost: float,
    threshold: float,
    message: str,
) -> "CostWarningEvent":
    """创建成本预警事件"""
    return CostWarningEvent(
        executionId=executionId,
        currentCost=currentCost,
        threshold=threshold,
        message=message,
    )


def create_resource_limit_event(
    executionId: str,
    limitType: str,
    current: int,
    limit: int,
    message: str,
) -> "ResourceLimitEvent":
    """创建资源限制事件"""
    return ResourceLimitEvent(
        executionId=executionId,
        limitType=limitType,
        current=current,
        limit=limit,
        message=message,
    )


# ============================================================================
# 自动执行触发事件工厂函数（已废弃，保留用于向后兼容）
# ============================================================================


def create_auto_execute_triggered_event(
    projectId: str,
    taskId: str,
    timestamp: datetime,
) -> "AutoExecuteTriggeredEvent":
    """创建自动执行触发事件 [已废弃]"""
    return AutoExecuteTriggeredEvent(
        projectId=projectId,
        taskId=taskId,
        timestamp=timestamp,
    )
