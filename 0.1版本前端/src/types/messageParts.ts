/**
 * 统一消息 Part 类型定义
 *
 * 设计原则：
 * - 每个 Part 有独立的 state 字段（streaming/done），前端精确控制渲染
 * - Part 按数组顺序渲染（= 追加顺序 = 接收顺序），sequence 仅用于历史消息指纹去重
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
  /** 仅历史消息（API 映射）使用；流式新建的 part 不赋值，渲染按数组顺序 */
  sequence?: number
}

/** 思考过程 Part */
export interface ThinkingPart {
  type: 'thinking'
  content: string
  state: PartState
  /** 仅历史消息（API 映射）使用；流式新建的 part 不赋值，渲染按数组顺序 */
  sequence?: number
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
  /** 结构化完整结果数据（后端 tool_result 事件的 result_data），供工具卡片渲染 diff 等；
   *  result 字段为截断预览字符串，resultData 携带完整结构 */
  resultData?: unknown
  error?: string
  durationMs?: number
  /** 仅历史消息（API 映射）使用；流式新建的 part 不赋值，渲染按数组顺序 */
  sequence?: number
  progress?: number
  /** 当前执行步骤描述 */
  currentStep?: string
  /** 所属任务容器 ID（用于解析工具卡片的文件路径） */
  containerTaskId?: string
}

/** 系统通知 Part */
export interface SystemPart {
  type: 'system'
  content: string
  level: SystemLevel
  notificationType: string
  /** 仅历史消息（API 映射）使用；流式新建的 part 不赋值，渲染按数组顺序 */
  sequence?: number
}

/** 统一 Part 联合类型 */
export type MessagePart = TextPart | ThinkingPart | ToolCallPart | SystemPart
