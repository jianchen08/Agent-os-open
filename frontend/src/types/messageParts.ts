/**
 * 统一消息 Part 类型定义
 *
 * 设计原则：
 * - 每个 Part 有独立的 state 字段（streaming/done），前端精确控制渲染
 * - Part 按 sequence 排序，支持 text/thinking/tool_call 交错显示
 * - 流式增量通过 appendToPart 追加，不需要 reconcile
 */

/** Part 状态：流式中 / 完成 */
export type PartState = 'streaming' | 'done'

/** 工具调用状态：比 PartState 更细粒度 */
export type ToolCallPartState = 'streaming' | 'calling' | 'done' | 'error' | 'cancelled'

/** 系统通知级别 */
export type SystemLevel = 'info' | 'warning' | 'error'

/** 文本 Part */
export interface TextPart {
  type: 'text'
  content: string
  state: PartState
  sequence: number
}

/** 思考过程 Part */
export interface ThinkingPart {
  type: 'thinking'
  content: string
  state: PartState
  sequence: number
  durationMs?: number
  steps?: import('./models').ThinkingStep[]
}

/** 工具调用 Part */
export interface ToolCallPart {
  type: 'tool_call'
  callId: string
  name: string
  args: Record<string, unknown>
  state: ToolCallPartState
  result?: unknown
  error?: string
  durationMs?: number
  sequence: number
  progress?: number
  /** 当前执行步骤描述 */
  currentStep?: string
}

/** 系统通知 Part */
export interface SystemPart {
  type: 'system'
  content: string
  level: SystemLevel
  notificationType: string
  sequence: number
}

/** 统一 Part 联合类型 */
export type MessagePart = TextPart | ThinkingPart | ToolCallPart | SystemPart
