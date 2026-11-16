/**
 * 系统级通知决策层（消息侧唯一出口）
 *
 * 消息到达时"是否弹系统 toast"的决策 + 弹出调用。桥接复用
 * utils/systemNotification（Electron notification:show 封装：免打扰开关、
 * 永不 reject），本模块不直接触达 IPC——全前端系统通知仍只有一个壳调用面。
 *
 * 接线面分工：
 * - 交互卡路径不经此处：useInteractionHandler 在卡片落 store 时直连
 *   utils/systemNotification 无条件弹（前台也弹；/interaction/pending 恢复
 *   重放不弹是既有反刷屏设计，见该文件入列复核注释）。
 * - 消息路径（触发器主动消息 / 普通助手消息）经本模块，由 messageHandler
 *   在 new_message 落定时调用。
 *
 * 决策规则 v3（判定器驱动，前端零启发式）：
 * - 消息 metadata 携带 notify_worthy===true（后端判定器认定值得打断：里程碑/
 *   报错/提问/提醒）→ 弹（无视可见性/焦点，前台也弹——判定器已替用户做过
 *   "值不值得打断"决策）
 * - 其余一律静默不弹（无论可见性/焦点）：无标记的后台过程碎语、未打标的
 *   注入轮都不再前端兜底弹出——防 away 时任务管道吐 N 条消息轰炸；触发器/
 *   主动消息是否弹出由判定器打标接管，proactive 判据不再参与前端决策
 */

import { showSystemNotification } from '@/utils/systemNotification'

/** 系统通知正文截断上限（字符） */
export const SYSTEM_NOTIFY_BODY_MAX_CHARS = 80

/** 消息侧系统通知决策输入 */
export interface MessageNotifyInput {
  /** 消息 metadata.notify_worthy===true（后端判定器标记，键名契约）：唯一弹出依据 */
  notifyWorthy?: boolean
}

/**
 * 消息到达是否弹系统通知（纯决策，便于测试与后续配置化）。
 *
 * v3：标记 = 弹，无标记 = 静默——可见性/焦点兜底已撤销（后台不弹，防管道
 * 消息轰炸）。严格 === true：后端契约只认 boolean true，其余值视作无标记。
 */
export function shouldNotifyOnMessage(input: MessageNotifyInput): boolean {
  return input.notifyWorthy === true
}

/**
 * 从消息提取通知正文文本：优先顶层 content，回落 text parts 拼接。
 * 无文本（纯工具调用轮等）返回空串，由调用方跳过。
 */
export function extractNotifyText(message: {
  content?: unknown
  parts?: Array<{ type?: string; content?: unknown }>
}): string {
  if (typeof message.content === 'string' && message.content.trim() !== '') {
    return message.content
  }
  const parts = Array.isArray(message.parts) ? message.parts : []
  return parts
    .filter((p) => p?.type === 'text' && typeof p.content === 'string')
    .map((p) => p.content as string)
    .join('')
}

/** 折叠空白后截断到上限（正文只做预览，不带省略号修饰） */
export function truncateNotifyBody(text: string): string {
  return text.replace(/\s+/g, ' ').trim().slice(0, SYSTEM_NOTIFY_BODY_MAX_CHARS)
}

/**
 * 已通知消息 id 记忆：同 id 重复投递（重连重放 / 对账重入）只决策一次——
 * 首次投递判"不弹"的重放也不弹（重放不是新活动）。有界防长会话无界增长。
 */
const notifiedMessageIds = new Set<string>()
const NOTIFIED_ID_CAP = 500

function rememberNotifiedId(id: string): void {
  notifiedMessageIds.add(id)
  while (notifiedMessageIds.size > NOTIFIED_ID_CAP) {
    const oldest = notifiedMessageIds.values().next().value
    if (oldest === undefined) break
    notifiedMessageIds.delete(oldest)
  }
}

/** 清空已通知记忆（测试用） */
export function resetSystemNotifierForTest(): void {
  notifiedMessageIds.clear()
}

/**
 * 消息落定后的系统通知出口：决策 + 弹出（幂等：同 dedupeKey 只处理一次）。
 *
 * @returns 是否实际弹出。失败（非 Electron / 已静音 / 桥接异常）折算 false，
 *          永不 reject——调用方 free to void，不阻塞消息流。
 */
export async function notifyPipelineMessage(opts: {
  /** 消息 metadata.notify_worthy 标记（后端判定器）：唯一弹出依据 */
  notifyWorthy?: boolean
  title: string
  body: string
  /** 消息归属 thread_id：透传给壳层通知，点击跳转对应对话；缺省退化仅聚焦 */
  threadId?: string
  /** 幂等键（assistant 消息 id）；缺省不去重 */
  dedupeKey?: string
}): Promise<boolean> {
  const body = truncateNotifyBody(opts.body)
  if (body === '') return false

  if (opts.dedupeKey) {
    if (notifiedMessageIds.has(opts.dedupeKey)) return false
    rememberNotifiedId(opts.dedupeKey)
  }

  if (!shouldNotifyOnMessage({ notifyWorthy: opts.notifyWorthy })) return false

  return showSystemNotification({ title: opts.title, body, threadId: opts.threadId })
}
