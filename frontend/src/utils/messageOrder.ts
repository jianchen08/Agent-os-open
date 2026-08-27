/**
 * 消息时序排序（渲染层/数据层共用排序键）。
 *
 * 排序键优先级：sequence → timestamp → id。sequence 是管道内权威序号（后端
 * 分配），流式占位在 stream_start 时无权威 seq（挂空 → 排末尾），stream_end
 * 的 final_sequence 到达后由对账纠正；timestamp 为第二键（乐观 user 无 seq，
 * 按发送时间落位）；id 为稳定兜底键，保证 sequence/timestamp 相同时排序稳定。
 *
 * [来源: deepseek-harness-rc8 chat-snapshot-builder.orderedVisible] 渲染层
 * 兜底：DSH 在 view 层按 anchorSeq 排序后再渲染，节点 store 写入顺序与渲染
 * 顺序解耦。agentos 同款——MessageList 渲染前先按本函数排序，事件乱序/延迟
 * 到达不再影响最终渲染时序。
 */

import type { Message } from '@/types/models'

export function compareMessages(a: Message, b: Message): number {
  const seqA = a.sequence ?? Number.MAX_SAFE_INTEGER
  const seqB = b.sequence ?? Number.MAX_SAFE_INTEGER
  if (seqA !== seqB) {
    return seqA - seqB
  }
  const timeDiff = new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  if (timeDiff !== 0) {
    return timeDiff
  }
  // 第三级排序用 id，确保排序稳定
  const idA = a.id || ''
  const idB = b.id || ''
  return idA < idB ? -1 : idA > idB ? 1 : 0
}
