/**
 * 流式总耗时锚点解析（2026-11-06 用户裁定）：思考中/执行中指示行的实时
 * 总耗时自触发消息发出起计——锚点 = 同清单内该消息之前最近一条 user 消息
 * 的发出时刻，到整条消息结束为止（圈随消息终态消失，计时同葬）。
 */
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
