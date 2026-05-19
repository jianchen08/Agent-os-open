/**
 * 统一消息格式系统 — 前端类型定义
 *
 * 与后端 src/schemas/message.py 保持镜像同步。
 * 所有 WebSocket 推送消息和 HTTP API 响应共用此结构。
 */

// =============================================================================
// 枚举
// =============================================================================

/**
 * 消息类型枚举（AC1: 覆盖所有状态）
 *
 * 与后端 MessageType 一一对应
 */
export enum MessageType {
  THINKING = 'thinking',
  EXECUTING = 'executing',
  WAITING = 'waiting',
  COMPLETED = 'completed',
  FAILED = 'failed',
  CANCELLED = 'cancelled',
}

/**
 * 消息子类型枚举
 *
 * 与后端 MessageSubtype 一一对应
 */
export enum MessageSubtype {
  TEXT = 'text',
  ERROR = 'error',
  PROGRESS = 'progress',
  STATUS = 'status',
  SYSTEM = 'system',
}

// =============================================================================
// 模型
// =============================================================================

/**
 * 统一消息模型（AC2: WebSocket 和 HTTP API 使用相同结构）
 *
 * 与后端 UnifiedMessage 一一对应
 */
export interface UnifiedMessage {
  /** 消息类型 */
  type: MessageType
  /** 消息子类型（可选） */
  subtype: MessageSubtype | null
  /** 状态字符串（AC4: 默认跟随 type 的枚举值） */
  status: string
  /** 消息内容 */
  content: Record<string, unknown>
  /** ISO 8601 时间戳（AC3: 带时区） */
  timestamp: string
  /** 元数据（task_id, agent_id, session_id 等） */
  metadata: Record<string, string>
}

// =============================================================================
// 前端 UI 状态映射（AC5）
// =============================================================================

/**
 * UI 状态映射项
 */
export interface MessageTypeUIMap {
  /** 主题色（Tailwind CSS 色值） */
  color: string
  /** 图标名称 */
  icon: string
  /** 中文标签 */
  label: string
}

/**
 * 消息类型 → 前端 UI 状态映射
 *
 * 与后端 MESSAGE_TYPE_UI_MAP 一一对应
 */
export const MESSAGE_TYPE_UI_MAP: Record<MessageType, MessageTypeUIMap> = {
  [MessageType.THINKING]: {
    color: '#6366f1', // indigo-500
    icon: 'brain',
    label: '思考中',
  },
  [MessageType.EXECUTING]: {
    color: '#3b82f6', // blue-500
    icon: 'play',
    label: '执行中',
  },
  [MessageType.WAITING]: {
    color: '#f59e0b', // amber-500
    icon: 'clock',
    label: '等待中',
  },
  [MessageType.COMPLETED]: {
    color: '#22c55e', // green-500
    icon: 'check-circle',
    label: '已完成',
  },
  [MessageType.FAILED]: {
    color: '#ef4444', // red-500
    icon: 'x-circle',
    label: '失败',
  },
  [MessageType.CANCELLED]: {
    color: '#9ca3af', // gray-400
    icon: 'ban',
    label: '已取消',
  },
} as const

// =============================================================================
// 工具函数
// =============================================================================

/**
 * 验证消息字典是否为有效的 UnifiedMessage
 *
 * @param data - 待验证的消息字典
 * @returns true 表示验证通过
 */
export function isValidMessageType(value: string): value is MessageType {
  return Object.values(MessageType).includes(value as MessageType)
}

/**
 * 获取消息类型对应的 UI 映射
 *
 * @param type - 消息类型
 * @returns UI 映射对象
 */
export function getMessageTypeUI(type: MessageType): MessageTypeUIMap {
  return MESSAGE_TYPE_UI_MAP[type]
}

/**
 * 从原始字典解析 UnifiedMessage
 *
 * @param data - 原始消息字典
 * @returns UnifiedMessage 实例，或 null（如果格式无效）
 */
export function parseUnifiedMessage(
  data: Record<string, unknown>,
): UnifiedMessage | null {
  if (!data.type || !isValidMessageType(data.type as string)) {
    return null
  }
  return {
    type: data.type as MessageType,
    subtype: data.subtype
      ? (data.subtype as MessageSubtype)
      : null,
    status: (data.status as string) || (data.type as string),
    content: (data.content as Record<string, unknown>) || {},
    timestamp: (data.timestamp as string) || new Date().toISOString(),
    metadata: (data.metadata as Record<string, string>) || {},
  }
}
