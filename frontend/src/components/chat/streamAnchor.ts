/**
 * 流式总耗时锚点解析（2026-11-06 用户裁定）：思考中/执行中指示行的实时
 * 总耗时自触发消息发出起计——锚点 = 同清单内该消息之前最近一条 user 消息
 * 的发出时刻，到整条消息结束为止（圈随消息终态消失，计时同葬）。
 */
import { getRunStartedAt } from '@/utils/streamRunAnchor'
import type { Message } from '@/types/models'

/**
 * 解析流式消息的总耗时锚点。用户发送与通知/触发注入（ensureInjectedUserMessage
 * 补插 user 气泡）都在 assistant 之前，天然覆盖两类触发源；一次发送驱动的
 * 多轮 assistant 各轮圈计的都是自触发消息起的总时长。
 */
export function resolveStreamAnchorMs(
  messages: Message[] | undefined | null,
  messageId: string,
): number | null {
  if (!messages) return null
  const idx = messages.findIndex((m) => m.id === messageId)
  if (idx < 0) return null
  for (let i = idx - 1; i >= 0; i--) {
    if (messages[i].role !== 'user') continue
    const ts = Date.parse(messages[i].timestamp)
    return Number.isFinite(ts) ? ts : null
  }
  return null
}

/**
 * 显示锚点（消费口）：user 消息锚点与 run 执行起点中较新者。
 * 重新生成/回退/编辑重发不产生新 user 消息（内核截断重跑，旧 user 时间戳
 * 不变），run 起点较新时接管锚点（2026-11-07 用户纠偏）；队列续发 run 未换，
 * 后发的 user 消息较新时仍归位各自触发消息；run 起点缺席（刷新后未登记）
 * 回落 user 消息锚点。
 */
export function resolveStreamDisplayAnchorMs(
  messages: Message[] | undefined | null,
  messageId: string,
  pipelineId?: string | null,
): number | null {
  const userAnchor = resolveStreamAnchorMs(messages, messageId)
  const runAnchor = pipelineId ? (getRunStartedAt(pipelineId) ?? null) : null
  if (userAnchor != null && runAnchor != null) return Math.max(userAnchor, runAnchor)
  return userAnchor ?? runAnchor
}
