/**
 * 统一消息类型定义
 *
 * 定义统一消息格式，支持新的统一事件格式
 */

/**
 * 文件附件类型
 */
export interface Attachment {
  /** 文件类型 */
  type: string
  /** 文件 URL */
  url: string
  /** 文件名称 */
  name: string
}

/**
 * 统一输入消息格式（前端 → 后端）
 *
 * 对应设计文档中的 UnifiedIncomingMessage 接口
 */
export interface UnifiedIncomingMessage {
  /** 消息类型 */
  type: 'user_input' | 'command' | 'system_event'
  /** 消息内容 */
  content: string
  /** 会话ID */
  thread_id: string
  /** 用户ID */
  user_id: string
  /** 消息ID（可选，后端生成） */
  message_id?: string
  /** 元数据 */
  metadata?: {
    /** 是否启用思考模式 */
    enable_thinking?: boolean
    /** 附件列表 */
    attachments?: Attachment[]
    /** 其他自定义元数据 */
    [key: string]: unknown
  }
  /** 时间戳（ISO 8601 格式） */
  timestamp: string
}

/**
 * 统一流式事件类型枚举
 *
 * 对应设计文档中的 UnifiedStreamEvent.event_type
 */
export type UnifiedEventType =
  | 'stream.chunk'
  | 'stream.end'
  | 'stream.error'
  | 'tool.start'
  | 'tool.progress'
  | 'tool.end'
  | 'thinking.start'
  | 'thinking.chunk'
  | 'thinking.end'

/**
 * 统一流式事件载荷（Payload）
 *
 * 根据不同的事件类型，载荷内容有所不同
 */
export interface UnifiedEventPayload {
  // stream.chunk 专用
  content?: string

  // tool.start / tool.progress / tool.end 专用
  tool_name?: string
  args?: Record<string, unknown>
  progress?: number
  current_step?: string
  result?: unknown
  error?: string

  // thinking.chunk 专用
  thinking_content?: string

  // stream.end 专用
  final_content?: string
  total_chunks?: number
  duration_ms?: number

  // stream.error 专用
  error_code?: string
  error_message?: string

  // 其他自定义字段
  [key: string]: unknown
}

/**
 * 统一流式事件元数据
 */
export interface UnifiedEventMetadata {
  /** 片段序号 */
  chunk_index?: number
  /** 时间戳（ISO 8601 格式） */
  timestamp: string
  /** 来源适配器ID */
  adapter_id?: string
  /** 其他自定义元数据 */
  [key: string]: unknown
}

/**
 * 统一流式事件格式（后端 → 前端）
 *
 * 对应设计文档中的 UnifiedStreamEvent 接口
 */
export interface UnifiedStreamEvent {
  /** 事件类型 */
  event_type: UnifiedEventType
  /** 消息ID */
  message_id: string
  /** 会话ID */
  thread_id: string
  /** 事件载荷 */
  payload: UnifiedEventPayload
  /** 事件元数据 */
  metadata: UnifiedEventMetadata
}

/**
 * 统一事件处理器订阅器类型
 */
export type UnifiedEventSubscriber<T extends UnifiedEventType> = (
  event: UnifiedStreamEvent & { event_type: T },
) => void

/**
 * 统一事件处理器选项
 */
export interface UnifiedEventHandlerOptions {
  /** 是否启用自动重连 */
  autoReconnect?: boolean
  /** 是否启用日志记录 */
  enableLogging?: boolean
  /** 消息超时时间（毫秒） */
  messageTimeout?: number
}
