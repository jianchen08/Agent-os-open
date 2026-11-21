/**
 * 任务现场活动抽取（纯函数，无 store/网络依赖）
 *
 * 现场页时间轴的数据面：把管道消息（`pipelineMessageStore.messagesByPipeline`）
 * 里的 `tool_call` part 抽成按时间顺序的活动条目。**消息面是复盘主干**
 * （方案 §3.7 三重证据：traces 90 天保留、同 step 只留末轮、不含 staging 键，
 * 且完整命令行只在消息面 args）。
 *
 * 抽取只做「形状归一」，不渲染、不取流、不做 diff——那些在视图层复用既有件。
 */

import { deriveSceneCategory, describeToolAction } from '@/utils/toolCategory'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'
import type { Message } from '@/types/models'
import type { SceneToolCategory } from '@/utils/toolCategory'

/** 现场活动条目（一行时间轴的完整数据） */
export interface SceneActivity {
  /** 工具调用唯一 ID（同步/复盘对齐键） */
  callId: string
  /** 工具名（原样，类别派生与卡型路由都用它） */
  toolName: string
  /** 工具参数（原样，展开区/动作文案都用它） */
  args: Record<string, unknown>
  /** 部分状态：streaming/calling/done/error/cancelled 原样透传 */
  state: ToolCallPart['state']
  /** 派生类别（terminal/browser/desktop/phone/software/file/other） */
  category: SceneToolCategory
  /** 一行文案（动作 + 目标，见 toolCategory） */
  summary: string
  /** 终态结果（可能是截断标记，展示层按既有契约解析） */
  result?: unknown
  /** 结构化结果（同上） */
  resultData?: unknown
  /** 错误（统一信封或旧形态字符串） */
  error?: ToolCallPart['error']
  /** 耗时（毫秒；缺省 = 未知，展示 '--'） */
  durationMs?: number
  /** 执行中实时输出增量 */
  partialOutput?: string[]
  /** 起始时间（ISO；缺省取所属消息 timestamp） */
  startedAt?: string
  /** 所属消息 ID（回取截断结果/定位用） */
  messageId: string
  /** 所属消息序号（同 callId 排序的稳定性补充） */
  messageSequence?: number
  /** 工具卡片文件名坐标（展开区打开文件用） */
  containerTaskId?: string
}

/**
 * 消息列表 → 现场活动列表（按消息顺序 + part 顺序，天然时序）。
 *
 * 只取 `tool_call` part；无 parts 的历史消息（老持久化形态）跳过——现场页
 * 宁可少条目也不猜数据形状。
 */
export function extractSceneActivities(messages: readonly Message[]): SceneActivity[] {
  const out: SceneActivity[] = []
  for (const message of messages) {
    const parts = message.parts
    if (!parts || parts.length === 0) continue
    for (const part of parts) {
      const activity = toolCallPartToSceneActivity(part, message)
      if (activity) out.push(activity)
    }
  }
  return out
}

/** 单 part → 活动条目（非 tool_call 返回 null） */
export function toolCallPartToSceneActivity(
  part: MessagePart,
  message: Message,
): SceneActivity | null {
  if (part.type !== 'tool_call') return null
  const toolCall = part as ToolCallPart
  const toolName = typeof toolCall.name === 'string' ? toolCall.name : ''
  const args = isRecord(toolCall.args) ? toolCall.args : {}
  return {
    callId: toolCall.callId,
    toolName,
    args,
    state: toolCall.state,
    category: deriveSceneCategory(toolName),
    summary: describeToolAction(toolName, args),
    ...(toolCall.result !== undefined ? { result: toolCall.result } : {}),
    ...(toolCall.resultData !== undefined ? { resultData: toolCall.resultData } : {}),
    ...(toolCall.error !== undefined ? { error: toolCall.error } : {}),
    ...(typeof toolCall.durationMs === 'number' ? { durationMs: toolCall.durationMs } : {}),
    ...(toolCall.partialOutput && toolCall.partialOutput.length > 0
      ? { partialOutput: toolCall.partialOutput }
      : {}),
    ...(message.timestamp ? { startedAt: message.timestamp } : {}),
    messageId: message.id,
    ...(typeof message.sequence === 'number' ? { messageSequence: message.sequence } : {}),
    ...(toolCall.containerTaskId ? { containerTaskId: toolCall.containerTaskId } : {}),
  }
}

/**
 * 按类别筛选（空集合 = 不过滤）。
 *
 * 返回新数组（不改入参），视图层可安全 memo 化。
 */
export function filterByCategories(
  activities: readonly SceneActivity[],
  categories: ReadonlySet<SceneToolCategory>,
): SceneActivity[] {
  if (categories.size === 0) return [...activities]
  return activities.filter((a) => categories.has(a.category))
}

/** 出现在活动列表里的类别（保持既有顺序，用于筛选条只显示有内容的类别） */
export function presentCategories(activities: readonly SceneActivity[]): SceneToolCategory[] {
  const seen = new Set<SceneToolCategory>()
  for (const activity of activities) seen.add(activity.category)
  return Array.from(seen)
}

/**
 * 各类别条目数（筛选条计数徽标用，全 7 类齐返回）。
 *
 * 筛选条按全类别渲染（无内容的类别显示 0 并可点出空态），但计数必须来自真实活动
 * ——「0」与「没统计」是两种事实，缺省不猜。
 */
export function countByCategory(
  activities: readonly SceneActivity[],
): Record<SceneToolCategory, number> {
  const counts: Record<SceneToolCategory, number> = {
    terminal: 0,
    browser: 0,
    desktop: 0,
    phone: 0,
    software: 0,
    file: 0,
    other: 0,
  }
  for (const activity of activities) counts[activity.category] += 1
  return counts
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
