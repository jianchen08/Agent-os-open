/**
 * 管道消息持久化裁剪（纯函数，自 pipelineMessageStore 抽出——
 * 冻结巨型文件只许缩小不许增长，批次F门禁）。
 *
 * 仅影响落盘数据，内存中的 messagesByPipeline 不受影响；
 * 被淘汰管道刷新后由 API 冷启动重新加载。
 */
import { compareMessages } from '@/utils/messageOrder'
import type { Message } from '@/types/models'

/**
 * 每个管道持久化的最大消息条数（IndexedDB 容量充裕，250 给单会话充足历史缓存）。
 * 内存上限 MAX_MESSAGES_PER_PIPELINE_IN_MEMORY=2000 始终 ≥ 此值，避免「内存裁掉但还想落盘」的矛盾。
 */
const PERSIST_MAX_MESSAGES_PER_PIPELINE = 250

/**
 * 持久化数据的总体积上限（100 MB）。
 * IndexedDB 容量充裕，但仍需上限防止无限增长吃满用户磁盘。
 * 超过时按 LRU 淘汰最不活跃的管道（见 trimMessagesForPersistence），内存数据不动，
 * 被淘汰管道刷新后从 API 重载。
 */
const PERSIST_MAX_TOTAL_BYTES = 100 * 1024 * 1024

/** 导出供测试断言用（生产代码不应依赖具体数值） */
export const _PERSIST_LIMITS = {
  maxMessagesPerPipeline: PERSIST_MAX_MESSAGES_PER_PIPELINE,
  maxTotalBytes: PERSIST_MAX_TOTAL_BYTES,
} as const

/** 裁剪每个 pipeline 的消息列表，仅保留最近 N 条用于持久化 */
function trimMessagesByCount(
  messagesByPipeline: Record<string, Message[]>,
): Record<string, Message[]> {
  const result: Record<string, Message[]> = {}
  for (const [pipelineId, msgs] of Object.entries(messagesByPipeline)) {
    if (!msgs || msgs.length === 0) continue
    // 按 sequence 排序后取最后 N 条（sequence 大=新）
    const sorted = [...msgs].sort(compareMessages)
    result[pipelineId] =
      sorted.length > PERSIST_MAX_MESSAGES_PER_PIPELINE
        ? sorted.slice(-PERSIST_MAX_MESSAGES_PER_PIPELINE)
        : sorted
  }
  return result
}

/**
 * 计算持久化对象的字节体积（UTF-16 近似，与 localStorage 配额口径一致，足够用于阈值判断）。
 * 逐管道累加，避免一次性 stringify 整个大对象造成额外开销。
 */
function estimatePersistedBytes(messagesByPipeline: Record<string, Message[]>): number {
  let total = 0
  for (const msgs of Object.values(messagesByPipeline)) {
    if (!msgs || msgs.length === 0) continue
    total += JSON.stringify(msgs).length
  }
  return total
}

/**
 * 获取管道最近活跃时间：取该管道最新一条消息的 timestamp。
 * 无消息或无时间戳返回 0（视为最不活跃，优先淘汰）。
 */
function pipelineLastActiveAt(msgs: Message[] | undefined): number {
  if (!msgs || msgs.length === 0) return 0
  let latest = 0
  for (const m of msgs) {
    const t = new Date(m.timestamp).getTime()
    if (!Number.isNaN(t) && t > latest) latest = t
  }
  return latest
}

/**
 * 持久化前的完整裁剪：先按单管道条数裁剪，再按全局总体积 LRU 淘汰最不活跃管道。
 *
 * LRU 排序规则：
 * 1. activePipelineId 始终排首位（绝不淘汰当前活跃管道）；
 * 2. 其余按最近活跃时间（最新消息 timestamp）降序，越久未活跃越靠后越先淘汰；
 * 3. 体积未超 PERSIST_MAX_TOTAL_BYTES 时原样返回（全留）。
 */
export function trimMessagesForPersistence(
  messagesByPipeline: Record<string, Message[]>,
  activePipelineId: string | null,
): Record<string, Message[]> {
  const byCount = trimMessagesByCount(messagesByPipeline)

  if (estimatePersistedBytes(byCount) <= PERSIST_MAX_TOTAL_BYTES) {
    return byCount
  }

  // 体积超限：按活跃度升序排列（最不活跃在前，优先淘汰），活跃管道始终保留
  const ranked = Object.entries(byCount).sort((a, b) => {
    // 活跃管道强制排最后（最不易被淘汰）
    if (a[0] === activePipelineId) return 1
    if (b[0] === activePipelineId) return -1
    return pipelineLastActiveAt(a[1]) - pipelineLastActiveAt(b[1])
  })

  // 从最不活跃的开始淘汰，直到总体积降到阈值内
  const kept: Record<string, Message[]> = {}
  let bytes = 0
  // 倒序取（活跃度高的先入选），保证先保留最活跃的
  for (let i = ranked.length - 1; i >= 0; i--) {
    const [pid, msgs] = ranked[i]
    const size = JSON.stringify(msgs).length
    // 活跃管道无论是否超限都保留；其余管道加入后若导致超限则跳过（淘汰）
    if (pid === activePipelineId || bytes + size <= PERSIST_MAX_TOTAL_BYTES) {
      kept[pid] = msgs
      bytes += size
    }
  }
  return kept
}
