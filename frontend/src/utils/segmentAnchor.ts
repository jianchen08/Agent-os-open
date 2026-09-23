/** 消息段锚点计算（消息段模型：‹i/n› 多代切换，方案 §2.5 后缀激活语义）
 *  [来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md] */

import type { SegmentMeta } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

/**
 * 消息 sequence 的段锚点 = 覆盖它的最新替换点：max base_seq ≤ seq。
 *
 * 后缀语义（§2.5）下激活 = [base_seq..末尾] 整段替换，故从锚点到序列末尾的
 * 全部消息同属该锚点（不按 base_len 截断——激活后启用序列可以长于冻结时的
 * 区间长度）。无更早替换点（纯追加退化形态）返回 null。
 */
export function anchorBaseSeqOf(segments: SegmentMeta[], seq: number): number | null {
  let anchor: number | null = null
  for (const seg of segments) {
    if (seg.base_seq <= seq && (anchor === null || seg.base_seq > anchor)) {
      anchor = seg.base_seq
    }
  }
  return anchor
}

/**
 * ‹i/n› 切换器锚点集合：messageId → 锚点 base_seq。
 *
 * - 宿主 = 该锚点覆盖范围内序列最晚的 assistant 消息（轮末条 assistant，
 *   orderedMessages 需已按 compareMessages 升序，后写覆盖即最晚）；
 * - 仅当锚点段数 >1 时收录（多代可切换才显示，§7.1「同 base_seq 多段」）；
 * - 无 sequence 的消息（乐观/流式占位）不参与锚定。
 */
export function collectSegmentSwitcherAnchors(
  orderedMessages: Message[],
  segments: SegmentMeta[],
): Map<string, number> {
  const anchors = new Map<string, number>()
  if (segments.length < 2) return anchors
  const lastAssistantByAnchor = new Map<number, Message>()
  for (const m of orderedMessages) {
    if (m.sequence == null || m.role !== 'assistant') continue
    const anchor = anchorBaseSeqOf(segments, m.sequence)
    if (anchor !== null) lastAssistantByAnchor.set(anchor, m)
  }
  for (const [baseSeq, message] of lastAssistantByAnchor) {
    const count = segments.filter((seg) => seg.base_seq === baseSeq).length
    if (count > 1) anchors.set(message.id, baseSeq)
  }
  return anchors
}
