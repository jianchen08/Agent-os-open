/**
 * WebSocket 标准消息格式定义
 *
 * ⚠️  此文件由 scripts/generate-websocket-types.py 自动生成
 * 请勿手动编辑！如需修改，请更新后端 src/api/websocket/message_types.py
 *
 * 统一前后端 WebSocket 消息格式，确保消息解析的一致性
 */

/**
 * 标准 WebSocket 消息格式
 */
export interface StandardWebSocketMessage {
  /** 消息类型 */
  type: string
  /** 消息ID（用于追踪和去重，由后端生成） */
  message_id?: string
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
    content: string // 用户输入内容
    attachments?: Array<Record<string, any>> // 文件附件（可选）
    enable_thinking?: boolean // 是否启用思考模式（可选）
  }
}

/**
 * 流式响应片段消息
 */
export interface StreamChunkMessage extends StandardWebSocketMessage {
  type: 'stream_chunk'
  data: {
    chunk: string // 文本片段
    ai_message_id: string // AI 消息 ID
  }
}

/**
 * 流式响应结束消息
 */
export interface StreamEndMessage extends StandardWebSocketMessage {
  type: 'stream_end'
  data: {
    ai_message_id: string // AI 消息 ID
    final_message_id: string // 最终消息 ID
    cancelled?: boolean // 是否被取消（可选）
  }
}

/**
 * 工具调用消息
 */
export interface ToolCallMessage extends StandardWebSocketMessage {
  type: 'tool_call'
  data: {
    tool_name: string // 工具名称
    parameters: Record<string, any> // 工具参数
    tool_call_id: string // 工具调用 ID
    ai_message_id: string // AI 消息 ID
  }
}

/**
 * 错误消息
 */
export interface ErrorMessage extends StandardWebSocketMessage {
  type: 'error'
  data: {
    error_code: string // 错误码
    message: string // 错误消息
    details?: Record<string, any> // 错误详情（可选）
  }
}

/**
 * 心跳消息
 */
export interface HeartbeatMessage extends StandardWebSocketMessage {
  type: 'heartbeat'
  data: {
    client_timestamp: string // 客户端时间戳
  }
}

/**
 * 心跳响应消息
 */
export interface HeartbeatAckMessage extends StandardWebSocketMessage {
  type: 'heartbeat_ack'
  data: {
    client_timestamp: string // 客户端时间戳
    server_timestamp: string // 服务端时间戳
  }
}

/**
 * 取消操作消息
 */
export interface CancelMessage extends StandardWebSocketMessage {
  type: 'cancel'
  data: {
    reason?: string // 取消原因（可选）
  }
}

/**
 * 连接建立确认消息
 */
export interface ConnectionEstablishedMessage extends StandardWebSocketMessage {
  type: 'connection_established'
  data: {
    connection_id: string // 连接 ID
    user_id: string // 用户 ID
    server_info?: Record<string, any> // 服务端信息（可选）
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
  | ErrorMessage
  | HeartbeatMessage
  | HeartbeatAckMessage
  | CancelMessage
  | ConnectionEstablishedMessage
  | MessageAckMessage
  | RequestMissedMessage

/**
 * 消息 ACK 确认消息
 */
export interface MessageAckMessage extends StandardWebSocketMessage {
  type: 'message_ack'
  data: {
    request_id: string // 被确认的消息 request_id
    received_at: string // 前端确认收到的时间戳
  }
}

/**
 * 请求遗漏消息
 */
export interface RequestMissedMessage extends StandardWebSocketMessage {
  type: 'request_missed'
  data: {
    last_received_request_id: string // 最后收到的消息 request_id
  }
}

/**
 * 消息类型枚举
 */
export const MessageTypes = {
  USER_INPUT: 'user_input',
  STREAM_CHUNK: 'stream_chunk',
  STREAM_END: 'stream_end',
  TOOL_CALL: 'tool_call',
  ERROR: 'error',
  HEARTBEAT: 'heartbeat',
  HEARTBEAT_ACK: 'heartbeat_ack',
  CANCEL: 'cancel',
  CONNECTION_ESTABLISHED: 'connection_established',
  THINKING_START: 'thinking_start',
  THINKING_CHUNK: 'thinking_chunk',
  THINKING_END: 'thinking_end',
  MESSAGE_ACK: 'message_ack',
  REQUEST_MISSED: 'request_missed',
} as const

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
  },
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
export function isStandardMessage(message: unknown): message is StandardMessage {
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

/**
 * 消息发送策略枚举
 */
export enum MessageSendStrategy {
  /** 直接发送，绕过队列（适用于心跳、取消等高优先级消息） */
  DIRECT = 'direct',
  /** 通过队列发送（适用于用户输入等需要保证顺序的消息） */
  QUEUED = 'queued',
}

/**
 * 消息发送配置
 */
export interface MessageSendConfig {
  /** 发送策略 */
  strategy: MessageSendStrategy
  /** 消息优先级（当使用队列时） */
  priority: number
}

/**
 * 消息类型与发送策略的映射配置
 *
 * 定义了每种消息类型应该使用的发送策略和优先级
 */
export const MESSAGE_CONFIG: Record<string, MessageSendConfig> = {
  // 心跳消息 - 直接发送，最高优先级
  heartbeat: {
    strategy: MessageSendStrategy.DIRECT,
    priority: 0,
  },

  // 取消操作 - 直接发送，最高优先级
  cancel: {
    strategy: MessageSendStrategy.DIRECT,
    priority: 0,
  },

  // 用户输入 - 队列发送，高优先级
  user_input: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 3,
  },

  // 流式消息 - 队列发送，普通优先级
  stream_chunk: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 2,
  },
  stream_end: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 2,
  },

  // 工具调用消息 - 队列发送，普通优先级
  tool_call: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 2,
  },

  // 错误消息 - 队列发送，高优先级
  error: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 3,
  },

  // 连接建立确认 - 队列发送，普通优先级
  connection_established: {
    strategy: MessageSendStrategy.QUEUED,
    priority: 1,
  },

  // ACK 确认 - 直接发送，最高优先级
  message_ack: {
    strategy: MessageSendStrategy.DIRECT,
    priority: 0,
  },

  // 请求遗漏消息 - 直接发送，最高优先级
  request_missed: {
    strategy: MessageSendStrategy.DIRECT,
    priority: 0,
  },
}
