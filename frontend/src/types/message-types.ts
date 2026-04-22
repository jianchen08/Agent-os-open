/**
 * WebSocket 标准消息格式定义
 *
 * ⚠️  此文件由 scripts/generate-message-types.py 自动生成
 * 请勿手动编辑！如需修改，请更新 docs/modules/message-types.md
 *
 * 统一前后端 WebSocket 消息格式，确保消息解析的一致性
 */

/**
 * 标准 WebSocket 消息格式
 */
export interface StandardWebSocketMessage {
  /** 消息类型 */
  type: string
  /** 消息ID（用于追踪和去重） */
  message_id?: string
  /** 临时ID（前端生成，用于流式消息关联） */
  temp_id?: string
  /** 线程ID */
  thread_id: string
  /** 时间戳 */
  timestamp: string
  /** 消息数据 */
  data: Record<string, any>
}

/**
 * 用户输入消息
 */
export interface UserInputMessage extends StandardWebSocketMessage {
  type: 'user_input'
  data: {
    [key: string]: any
  }
}

/**
 * 流式输出片段
 */
export interface StreamChunkMessage extends StandardWebSocketMessage {
  type: 'stream_chunk'
  data: {
    [key: string]: any
  }
}

/**
 * 流式输出结束
 */
export interface StreamEndMessage extends StandardWebSocketMessage {
  type: 'stream_end'
  data: {
    [key: string]: any
  }
}

/**
 * 工具调用消息（基础）
 */
export interface ToolCallMessage extends StandardWebSocketMessage {
  type: 'tool_call'
  data: {
    [key: string]: any
  }
}

/**
 * 工具调用开始
 */
export interface ToolCallStartMessage extends StandardWebSocketMessage {
  type: 'tool_call_start'
  data: {
    [key: string]: any
  }
}

/**
 * 工具调用进度
 */
export interface ToolCallProgressMessage extends StandardWebSocketMessage {
  type: 'tool_call_progress'
  data: {
    [key: string]: any
  }
}

/**
 * 工具调用输出
 */
export interface ToolCallOutputMessage extends StandardWebSocketMessage {
  type: 'tool_call_output'
  data: {
    [key: string]: any
  }
}

/**
 * 工具调用结束
 */
export interface ToolCallEndMessage extends StandardWebSocketMessage {
  type: 'tool_call_end'
  data: {
    [key: string]: any
  }
}

/**
 * 错误消息
 */
export interface ErrorMessage extends StandardWebSocketMessage {
  type: 'error'
  data: {
    [key: string]: any
  }
}

/**
 * 心跳消息
 */
export interface HeartbeatMessage extends StandardWebSocketMessage {
  type: 'heartbeat'
  data: {
    [key: string]: any
  }
}

/**
 * 心跳响应
 */
export interface HeartbeatAckMessage extends StandardWebSocketMessage {
  type: 'heartbeat_ack'
  data: {
    [key: string]: any
  }
}

/**
 * 取消操作消息
 */
export interface CancelMessage extends StandardWebSocketMessage {
  type: 'cancel'
  data: {
    [key: string]: any
  }
}

/**
 * 连接建立确认
 */
export interface ConnectionEstablishedMessage extends StandardWebSocketMessage {
  type: 'connection_established'
  data: {
    [key: string]: any
  }
}

/**
 * 审批决策消息
 */
export interface ApprovalMessage extends StandardWebSocketMessage {
  type: 'approval'
  data: {
    [key: string]: any
  }
}

/**
 * 执行控制消息（暂停/恢复/取消）
 */
export interface ExecutionControlMessage extends StandardWebSocketMessage {
  type: 'execution_control'
  data: {
    [key: string]: any
  }
}

/**
 * 用户输入响应（响应子 Agent 的输入请求）
 */
export interface UserInputResponseMessage extends StandardWebSocketMessage {
  type: 'user_input_response'
  data: {
    [key: string]: any
  }
}

/**
 * 记忆增强输入消息
 */
export interface MemoryEnhancedInputMessage extends StandardWebSocketMessage {
  type: 'memory_enhanced_input'
  data: {
    [key: string]: any
  }
}

/**
 * 上下文摘要消息
 */
export interface ContextSummaryMessage extends StandardWebSocketMessage {
  type: 'context_summary'
  data: {
    [key: string]: any
  }
}

/**
 * 记忆检索消息
 */
export interface MemoryRetrievalMessage extends StandardWebSocketMessage {
  type: 'memory_retrieval'
  data: {
    [key: string]: any
  }
}

/**
 * 记忆压缩消息
 */
export interface MemoryCompressionMessage extends StandardWebSocketMessage {
  type: 'memory_compression'
  data: {
    [key: string]: any
  }
}

/**
 * 状态变更
 */
export interface StateChangeMessage extends StandardWebSocketMessage {
  type: 'state_change'
  data: {
    [key: string]: any
  }
}

/**
 * 任务完成
 */
export interface TaskCompletedMessage extends StandardWebSocketMessage {
  type: 'task_completed'
  data: {
    [key: string]: any
  }
}

/**
 * 任务取消
 */
export interface TaskCancelledMessage extends StandardWebSocketMessage {
  type: 'task_cancelled'
  data: {
    [key: string]: any
  }
}

/**
 * 需要审批
 */
export interface ApprovalRequiredMessage extends StandardWebSocketMessage {
  type: 'approval_required'
  data: {
    [key: string]: any
  }
}

/**
 * 执行状态更新
 */
export interface ExecutionStatusUpdateMessage extends StandardWebSocketMessage {
  type: 'execution_status_update'
  data: {
    [key: string]: any
  }
}

/**
 * 思考开始
 */
export interface ThinkingStartMessage extends StandardWebSocketMessage {
  type: 'thinking_start'
  data: {
    [key: string]: any
  }
}

/**
 * 思考内容片段
 */
export interface ThinkingChunkMessage extends StandardWebSocketMessage {
  type: 'thinking_chunk'
  data: {
    [key: string]: any
  }
}

/**
 * 思考结束
 */
export interface ThinkingEndMessage extends StandardWebSocketMessage {
  type: 'thinking_end'
  data: {
    [key: string]: any
  }
}

/**
 * 思考过程更新（增量）
 */
export interface ThinkingUpdateMessage extends StandardWebSocketMessage {
  type: 'thinking_update'
  data: {
    [key: string]: any
  }
}

/**
 * 思考步骤更新
 */
export interface ThinkingStepUpdateMessage extends StandardWebSocketMessage {
  type: 'thinking_step_update'
  data: {
    [key: string]: any
  }
}

/**
 * 子 Agent 创建
 */
export interface SubAgentCreatedMessage extends StandardWebSocketMessage {
  type: 'sub_agent_created'
  data: {
    [key: string]: any
  }
}

/**
 * 子 Agent 等待输入
 */
export interface SubAgentWaitingInputMessage extends StandardWebSocketMessage {
  type: 'sub_agent_waiting_input'
  data: {
    [key: string]: any
  }
}

/**
 * 子 Agent 完成
 */
export interface SubAgentCompletedMessage extends StandardWebSocketMessage {
  type: 'sub_agent_completed'
  data: {
    [key: string]: any
  }
}

/**
 * Agent 层级变更
 */
export interface AgentLevelChangedMessage extends StandardWebSocketMessage {
  type: 'agent_level_changed'
  data: {
    [key: string]: any
  }
}

/**
 * 所有标准消息类型的联合类型
 */
export type StandardMessage =
  | UserInputMessage
  | StreamChunkMessage
  | StreamEndMessage
  | ToolCallMessage
  | ToolCallStartMessage
  | ToolCallProgressMessage
  | ToolCallOutputMessage
  | ToolCallEndMessage
  | ErrorMessage
  | HeartbeatMessage
  | HeartbeatAckMessage
  | CancelMessage
  | ConnectionEstablishedMessage
  | ApprovalMessage
  | ExecutionControlMessage
  | UserInputResponseMessage
  | MemoryEnhancedInputMessage
  | ContextSummaryMessage
  | MemoryRetrievalMessage
  | MemoryCompressionMessage
  | StateChangeMessage
  | TaskCompletedMessage
  | TaskCancelledMessage
  | ApprovalRequiredMessage
  | ExecutionStatusUpdateMessage
  | ThinkingStartMessage
  | ThinkingChunkMessage
  | ThinkingEndMessage
  | ThinkingUpdateMessage
  | ThinkingStepUpdateMessage
  | SubAgentCreatedMessage
  | SubAgentWaitingInputMessage
  | SubAgentCompletedMessage
  | AgentLevelChangedMessage

/**
 * 消息类型枚举
 */
export const MessageTypes = {
  AGENT_LEVEL_CHANGED: 'agent_level_changed',
  APPROVAL: 'approval',
  APPROVAL_REQUIRED: 'approval_required',
  CANCEL: 'cancel',
  CONNECTION_ESTABLISHED: 'connection_established',
  CONTEXT_SUMMARY: 'context_summary',
  ERROR: 'error',
  EXECUTION_CONTROL: 'execution_control',
  EXECUTION_STATUS_UPDATE: 'execution_status_update',
  HEARTBEAT: 'heartbeat',
  HEARTBEAT_ACK: 'heartbeat_ack',
  MEMORY_COMPRESSION: 'memory_compression',
  MEMORY_ENHANCED_INPUT: 'memory_enhanced_input',
  MEMORY_RETRIEVAL: 'memory_retrieval',
  STATE_CHANGE: 'state_change',
  STREAM_CHUNK: 'stream_chunk',
  STREAM_END: 'stream_end',
  SUB_AGENT_COMPLETED: 'sub_agent_completed',
  SUB_AGENT_CREATED: 'sub_agent_created',
  SUB_AGENT_WAITING_INPUT: 'sub_agent_waiting_input',
  TASK_CANCELLED: 'task_cancelled',
  TASK_COMPLETED: 'task_completed',
  THINKING_CHUNK: 'thinking_chunk',
  THINKING_END: 'thinking_end',
  THINKING_START: 'thinking_start',
  THINKING_STEP_UPDATE: 'thinking_step_update',
  USER_INPUT: 'user_input',
  USER_INPUT_RESPONSE: 'user_input_response',
} as const

/**
 * 消息类型的值类型
 */
export type MessageType = (typeof MessageTypes)[keyof typeof MessageTypes]

/**
 * 创建标准消息的工厂函数
 */
export function createStandardMessage<T extends StandardMessage>(
  type: T['type'],
  threadId: string,
  data: T['data'],
  options?: {
    messageId?: string
    timestamp?: string
  }
): T {
  return {
    type,
    thread_id: threadId,
    timestamp: options?.timestamp || new Date().toISOString(),
    data,
    ...(options?.messageId && { message_id: options.messageId }),
  } as T
}

/**
 * 验证消息格式是否符合标准
 */
export function isStandardMessage(message: any): message is StandardMessage {
  return (
    typeof message === 'object' &&
    message !== null &&
    typeof message.type === 'string' &&
    typeof message.thread_id === 'string' &&
    typeof message.timestamp === 'string' &&
    typeof message.data === 'object' &&
    message.data !== null
  )
}
