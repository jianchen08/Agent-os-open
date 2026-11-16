/**
 * 渲染用消息流水线（时序排序 → 连续 assistant 合并 → 已吸收 tool 过滤）
 *
 * 单一真值：主列表（MessageList.displayMessages）与压缩段原文还原
 * （CompressionSegmentGroup 的段成员行）共用同一流水线，保证「段成员显示
 * 与压缩前一致」（2026-10-17 用户裁定：被压缩区间消息原样保留显示）。
 * 抽取自 MessageList 内联实现，逻辑零改动。
 */

import { mergeConsecutiveAssistantMessages } from '@/services/api/session'
import { compareMessages } from '@/utils/messageOrder'
import type { Message } from '@/types/models'

/**
 * 已按时序排序的消息 → 渲染行：
 * 1. 合并连续 assistant 为一个气泡（工具调用链显示为一条消息）；
 * 2. 渲染层跳过"已被吸收"的 tool 消息：若该 tool 消息的 toolCallId 已出现在
 *    前一个 assistant 的 tool_call part（merge 注入了结果，渲染为 ActivityCard），
 *    则独立 tool 卡片冗余，跳过。与流式渲染保持一致（流式时 tool 结果直接更新
 *    assistant 的 tool_call part，无独立 tool 消息）。
 *    注意：仅在渲染层过滤，不改 store/merge 的原始数据，流式与刷新后行为一致。
 *    兜底：若 tool 消息无 toolCallId，或前一个 assistant 无对应 tool_call part，则保留渲染。
 */
/** 空 assistant 消息判据（残影过滤，聊天消息流权威化设计真值不变量 4 的渲染
 *  层兜底）：无内容、无工具调用、无思考、无错误信封的 assistant —— 历史存量
 *  「零产出中断」空槽（装机库近 4000 槽位实测 374 条）渲染为空气泡，统一在
 *  渲染行过滤。流式中的消息豁免（占位气泡从空内容开始增长）。 */
function isEmptyAssistantMessage(m: Message): boolean {
  if (m.role !== 'assistant' || m.status === 'streaming') return false
  if (typeof m.content === 'string' && m.content.trim()) return false
  if (m.error) return false
  if (m.toolResultData || m.toolCallId) return false
  if (!m.parts) return true
  return !m.parts.some((p) => p.type === 'tool_call' || p.type === 'thinking')
}

export function buildRenderableMessages(orderedMessages: Message[]): Message[] {
  const merged = mergeConsecutiveAssistantMessages(orderedMessages)
  const absorbedToolCallIds = new Set<string>()
  for (const m of merged) {
    if (m.role === 'assistant' && m.parts) {
      for (const p of m.parts) {
        if (p.type === 'tool_call' && p.callId) {
          absorbedToolCallIds.add(p.callId)
        }
      }
    }
  }
  return merged.filter((m) => {
    if (m.role !== 'tool') {
      // 空 assistant 残影过滤（见 isEmptyAssistantMessage）
      return !isEmptyAssistantMessage(m)
    }
    const tcId = m.toolCallId
    // 无 toolCallId 或未被吸收 → 保留（兜底，避免结果丢失）
    if (!tcId || !absorbedToolCallIds.has(tcId)) return true
    // 已被 assistant 的 tool_call part 吸收 → 跳过（避免与 ActivityCard 重复）
    return false
  })
}

/** 权威时序排序（sequence→timestamp→id）后取渲染行（两消费点同入口） */
export function toRenderableMessages(messages: Message[]): Message[] {
  return buildRenderableMessages([...messages].sort(compareMessages))
}
