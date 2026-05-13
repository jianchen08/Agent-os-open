"""
WebSocket 标准消息格式定义 - 精简版

消息类型从 40+ 精简到 22 个，删除了：
- 记忆系统消息（后端处理，不需要前端交互）
- 子 Agent 消息（共用主会话）
- 空壳功能（未实现）
- 冗余消息（功能重复）

统一前后端 WebSocket 消息格式，确保消息解析的一致性
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """WebSocket 消息类型枚举"""

    # ========== 客户端 → 服务器（8个）==========
    USER_INPUT = "user_input"  # 用户发送消息
    HEARTBEAT = "heartbeat"  # 心跳保活
    CANCEL = "cancel"  # 取消当前操作
    EXECUTION_CONTROL = "execution_control"  # 暂停/恢复/取消执行

    # 人类交互（客户端 → 服务器）
    INTERACTION_RESPONSE = "interaction_response"  # 提交交互响应
    CONVERSATION_MESSAGE = "conversation_message"  # 对话模式下的消息

    # ========== 服务器 → 客户端（20个）==========
    # 连接管理
    CONNECTION_ESTABLISHED = "connection_established"  # 连接建立确认
    HEARTBEAT_ACK = "heartbeat_ack"  # 心跳响应

    # 流式输出
    STREAM_START = "stream_start"  # 流式输出开始
    STREAM_CHUNK = "stream_chunk"  # 流式输出片段
    STREAM_END = "stream_end"  # 流式输出结束

    # 思考模式
    THINKING_START = "thinking_start"  # 思考模式开始
    THINKING_CHUNK = "thinking_chunk"  # 思考内容片段
    THINKING_END = "thinking_end"  # 思考模式结束

    # 工具调用
    TOOL_CALL_START = "tool_call_start"  # 工具调用开始
    TOOL_CALL_PROGRESS = "tool_call_progress"  # 工具调用进度
    TOOL_CALL_END = "tool_call_end"  # 工具调用结束

    # 状态和通知
    ERROR = "error"  # 错误消息
    NEW_MESSAGE = "new_message"  # 新消息通知
    TASK_COMPLETED = "task_completed"  # 任务完成
    STATE_CHANGE = "state_change"  # 状态变更
    MESSAGE_DELETED = "message_deleted"  # 消息删除通知
    MESSAGE_UPDATED = "message_updated"  # 消息更新通知
    EXECUTION_CONTROL_RESPONSE = "execution_control_response"  # 执行控制响应

    # 人类交互（服务器 → 客户端）
    INTERACTION_REQUEST = "interaction_request"  # 需要人类交互
    INTERACTION_CANCELLED = "interaction_cancelled"  # 交互请求已取消


class StandardWebSocketMessage(BaseModel):
    """标准 WebSocket 消息格式"""

    type: str = Field(..., description="消息类型")
    message_id: str | None = Field(None, description="消息ID（用于追踪和去重）")
    temp_id: str | None = Field(
        None, description="临时ID（前端生成，用于流式消息关联）"
    )
    thread_id: str = Field(..., description="线程ID")
    timestamp: str = Field(..., description="时间戳")
    data: dict[str, Any] = Field(..., description="消息数据")


# ========== 客户端消息模型 ==========


class UserInputMessage(StandardWebSocketMessage):
    """用户输入消息"""

    type: str = Field(default="user_input", description="消息类型")


class HeartbeatMessage(StandardWebSocketMessage):
    """心跳消息"""

    type: str = Field(default="heartbeat", description="消息类型")


class CancelMessage(StandardWebSocketMessage):
    """取消操作消息"""

    type: str = Field(default="cancel", description="消息类型")


class ExecutionControlMessage(StandardWebSocketMessage):
    """执行控制消息（暂停/恢复/取消）"""

    type: str = Field(default="execution_control", description="消息类型")


class InteractionResponseMessage(StandardWebSocketMessage):
    """交互响应消息（客户端 → 服务器）"""

    type: str = Field(default="interaction_response", description="消息类型")


class ConversationMessage(StandardWebSocketMessage):
    """对话模式下的消息（客户端 → 服务器）"""

    type: str = Field(default="conversation_message", description="消息类型")


# ========== 服务器消息模型 ==========


class ConnectionEstablishedMessage(StandardWebSocketMessage):
    """连接建立确认"""

    type: str = Field(default="connection_established", description="消息类型")


class HeartbeatAckMessage(StandardWebSocketMessage):
    """心跳响应"""

    type: str = Field(default="heartbeat_ack", description="消息类型")


class StreamStartMessage(StandardWebSocketMessage):
    """流式输出开始"""

    type: str = Field(default="stream_start", description="消息类型")


class StreamChunkMessage(StandardWebSocketMessage):
    """流式输出片段"""

    type: str = Field(default="stream_chunk", description="消息类型")


class StreamEndMessage(StandardWebSocketMessage):
    """流式输出结束"""

    type: str = Field(default="stream_end", description="消息类型")


class ThinkingStartMessage(StandardWebSocketMessage):
    """思考开始"""

    type: str = Field(default="thinking_start", description="消息类型")


class ThinkingChunkMessage(StandardWebSocketMessage):
    """思考内容片段"""

    type: str = Field(default="thinking_chunk", description="消息类型")


class ThinkingEndMessage(StandardWebSocketMessage):
    """思考结束"""

    type: str = Field(default="thinking_end", description="消息类型")


class ToolCallStartMessage(StandardWebSocketMessage):
    """工具调用开始"""

    type: str = Field(default="tool_call_start", description="消息类型")


class ToolCallProgressMessage(StandardWebSocketMessage):
    """工具调用进度"""

    type: str = Field(default="tool_call_progress", description="消息类型")


class ToolCallEndMessage(StandardWebSocketMessage):
    """工具调用结束"""

    type: str = Field(default="tool_call_end", description="消息类型")


class ErrorMessage(StandardWebSocketMessage):
    """错误消息"""

    type: str = Field(default="error", description="消息类型")


class NewMessageMessage(StandardWebSocketMessage):
    """新消息通知"""

    type: str = Field(default="new_message", description="消息类型")


class TaskCompletedMessage(StandardWebSocketMessage):
    """任务完成"""

    type: str = Field(default="task_completed", description="消息类型")


class StateChangeMessage(StandardWebSocketMessage):
    """状态变更（合并了 task_cancelled, execution_status_update）"""

    type: str = Field(default="state_change", description="消息类型")


class MessageDeletedMessage(StandardWebSocketMessage):
    """消息删除通知"""

    type: str = Field(default="message_deleted", description="消息类型")


class MessageUpdatedMessage(StandardWebSocketMessage):
    """消息更新通知"""

    type: str = Field(default="message_updated", description="消息类型")


class ExecutionControlResponseMessage(StandardWebSocketMessage):
    """执行控制响应"""

    type: str = Field(default="execution_control_response", description="消息类型")


class InteractionRequestMessage(StandardWebSocketMessage):
    """交互请求消息（服务器 → 客户端）"""

    type: str = Field(default="interaction_request", description="消息类型")


class InteractionCancelledMessage(StandardWebSocketMessage):
    """交互取消消息（服务器 → 客户端）"""

    type: str = Field(default="interaction_cancelled", description="消息类型")


# ========== 消息类型常量 ==========


class MessageTypes:
    """消息类型常量（推荐使用 MessageType 枚举）"""

    # 客户端 → 服务器
    USER_INPUT = MessageType.USER_INPUT.value
    HEARTBEAT = MessageType.HEARTBEAT.value
    CANCEL = MessageType.CANCEL.value
    EXECUTION_CONTROL = MessageType.EXECUTION_CONTROL.value
    INTERACTION_RESPONSE = MessageType.INTERACTION_RESPONSE.value
    CONVERSATION_MESSAGE = MessageType.CONVERSATION_MESSAGE.value

    # 服务器 → 客户端
    CONNECTION_ESTABLISHED = MessageType.CONNECTION_ESTABLISHED.value
    HEARTBEAT_ACK = MessageType.HEARTBEAT_ACK.value
    STREAM_START = MessageType.STREAM_START.value
    STREAM_CHUNK = MessageType.STREAM_CHUNK.value
    STREAM_END = MessageType.STREAM_END.value
    THINKING_START = MessageType.THINKING_START.value
    THINKING_CHUNK = MessageType.THINKING_CHUNK.value
    THINKING_END = MessageType.THINKING_END.value
    TOOL_CALL_START = MessageType.TOOL_CALL_START.value
    TOOL_CALL_PROGRESS = MessageType.TOOL_CALL_PROGRESS.value
    TOOL_CALL_END = MessageType.TOOL_CALL_END.value
    ERROR = MessageType.ERROR.value
    NEW_MESSAGE = MessageType.NEW_MESSAGE.value
    TASK_COMPLETED = MessageType.TASK_COMPLETED.value
    STATE_CHANGE = MessageType.STATE_CHANGE.value
    MESSAGE_DELETED = MessageType.MESSAGE_DELETED.value
    MESSAGE_UPDATED = MessageType.MESSAGE_UPDATED.value
    EXECUTION_CONTROL_RESPONSE = MessageType.EXECUTION_CONTROL_RESPONSE.value
    INTERACTION_REQUEST = MessageType.INTERACTION_REQUEST.value
    INTERACTION_CANCELLED = MessageType.INTERACTION_CANCELLED.value


# ========== 工厂函数 ==========


def create_standard_message(
    message_type: str,
    thread_id: str,
    data: dict[str, Any],
    message_id: str | None = None,
    temp_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建标准消息的工厂函数

    Args:
        message_type: 消息类型
        thread_id: 线程ID
        data: 消息数据
        message_id: 消息ID（可选）
        temp_id: 临时ID（可选）
        timestamp: 时间戳（可选，默认使用当前时间）

    Returns:
        标准格式的消息字典
    """
    # BUG-FIX: 在 data 中自动注入 pipeline_id，默认回退到 thread_id
    # 修复原因：前端依赖 pipeline_id 追踪消息流，部分调用方未显式传递该字段
    enriched_data = {**data, "pipeline_id": data.get("pipeline_id", thread_id)}

    message = {
        "type": message_type,
        "thread_id": thread_id,
        "timestamp": timestamp or datetime.utcnow().isoformat(),
        "data": enriched_data,
    }

    if message_id:
        message["message_id"] = message_id
    if temp_id:
        message["temp_id"] = temp_id

    return message


def create_tool_call_start_message(
    thread_id: str,
    tool_call_id: str,
    tool_name: str,
    parameters: dict[str, Any],
    ai_message_id: str,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """创建工具调用开始消息"""
    return create_standard_message(
        message_type=MessageType.TOOL_CALL_START.value,
        thread_id=thread_id,
        data={
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "parameters": parameters,
            "ai_message_id": ai_message_id,
        },
        timestamp=timestamp,
    )


def create_tool_call_end_message(
    thread_id: str,
    tool_call_id: str,
    status: str,
    result: Any | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    ai_message_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """创建工具调用结束消息"""
    data = {
        "tool_call_id": tool_call_id,
        "status": status,
    }

    if result is not None:
        data["result"] = result
    if error is not None:
        data["error"] = error
    if duration_ms is not None:
        data["duration_ms"] = duration_ms
    if ai_message_id is not None:
        data["ai_message_id"] = ai_message_id

    return create_standard_message(
        message_type=MessageType.TOOL_CALL_END.value,
        thread_id=thread_id,
        data=data,
        timestamp=timestamp,
    )


def create_state_change_message(
    thread_id: str,
    state: str,
    previous_state: str | None = None,
    reason: str | None = None,
    execution_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建状态变更消息（合并了 task_cancelled, execution_status_update）

    Args:
        thread_id: 线程ID
        state: 新状态 (running/paused/cancelled/completed/error)
        previous_state: 之前的状态
        reason: 状态变更原因
        execution_id: 执行ID
        timestamp: 时间戳

    Returns:
        状态变更消息
    """
    data = {"state": state}

    if previous_state is not None:
        data["previous_state"] = previous_state
    if reason is not None:
        data["reason"] = reason
    if execution_id is not None:
        data["execution_id"] = execution_id

    return create_standard_message(
        message_type=MessageType.STATE_CHANGE.value,
        thread_id=thread_id,
        data=data,
        timestamp=timestamp,
    )


def create_message_deleted_message(
    thread_id: str,
    message_id: str,
    deleted_count: int,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建消息删除通知

    Args:
        thread_id: 线程ID
        message_id: 被删除的消息ID
        deleted_count: 删除的消息总数
        timestamp: 时间戳

    Returns:
        消息删除通知消息
    """
    return create_standard_message(
        message_type=MessageType.MESSAGE_DELETED.value,
        thread_id=thread_id,
        data={
            "sessionId": thread_id,
            "messageId": message_id,
            "deletedCount": deleted_count,
        },
        timestamp=timestamp,
    )


def create_message_updated_message(
    thread_id: str,
    message_id: str,
    updated_fields: list[str],
    content: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建消息更新通知

    Args:
        thread_id: 线程ID
        message_id: 被更新的消息ID
        updated_fields: 更新的字段列表（如 ["content", "status"]）
        content: 新内容（如果更新了内容）
        timestamp: 时间戳

    Returns:
        消息更新通知消息
    """
    data = {
        "sessionId": thread_id,
        "messageId": message_id,
        "updatedFields": updated_fields,
    }

    if content is not None:
        data["content"] = content

    return create_standard_message(
        message_type=MessageType.MESSAGE_UPDATED.value,
        thread_id=thread_id,
        data=data,
        timestamp=timestamp,
    )


def create_interaction_request_message(
    thread_id: str,
    request_id: str,
    interaction_type: str,
    mode: str,
    title: str,
    description: str,
    priority: str = "normal",
    timeout: float = 300.0,
    approval_options: list[dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    conversation_context: dict[str, Any] | None = None,
    agent_id: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建交互请求消息（服务器 → 客户端）

    Args:
        thread_id: 线程ID
        request_id: 请求ID
        interaction_type: 交互类型 (approval/conversation)
        mode: 交互模式
        title: 请求标题
        description: 请求描述
        priority: 优先级
        timeout: 超时时间
        approval_options: 审批选项列表
        context: 交互上下文
        conversation_context: 对话上下文
        agent_id: Agent ID（用于前端跳转）
        timestamp: 时间戳

    Returns:
        交互请求消息
    """
    data = {
        "request_id": request_id,
        "interaction_type": interaction_type,
        "mode": mode,
        "title": title,
        "description": description,
        "priority": priority,
        "timeout": timeout,
    }

    if approval_options is not None:
        data["approval_options"] = approval_options
    if context is not None:
        data["context"] = context
    if conversation_context is not None:
        data["conversation_context"] = conversation_context
    if agent_id is not None:
        data["agent_id"] = agent_id

    return create_standard_message(
        message_type=MessageType.INTERACTION_REQUEST.value,
        thread_id=thread_id,
        data=data,
        timestamp=timestamp,
    )


def create_interaction_cancelled_message(
    thread_id: str,
    request_id: str,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict[str, Any]:
    """
    创建交互取消消息（服务器 → 客户端）

    Args:
        thread_id: 线程ID
        request_id: 请求ID
        reason: 取消原因
        timestamp: 时间戳

    Returns:
        交互取消消息
    """
    data = {"request_id": request_id}
    if reason is not None:
        data["reason"] = reason

    return create_standard_message(
        message_type=MessageType.INTERACTION_CANCELLED.value,
        thread_id=thread_id,
        data=data,
        timestamp=timestamp,
    )
