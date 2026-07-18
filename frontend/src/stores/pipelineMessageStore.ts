/** 统一管道消息状态管理 Store 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { getMessages as apiGetMessages, mergeConsecutiveAssistantMessages } from '@/services/api/session'
import { loggers } from '@/utils/logger'
import { indexedDbStorage } from '@/utils/indexedDbStorage'
// retry removed per audit: 内部 API 不应内置重试，429/5xx 重试统一由 axios interceptor 管理
import type { Message } from '@/types/models'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'

const logger = loggers.sessionStore

/**
 * 每个管道持久化的最大消息条数。
 * 迁移到 IndexedDB（GB 级容量）后从 50 提升至 250，给单会话充足历史缓存。
 * 内存上限 MAX_MESSAGES_PER_PIPELINE_IN_MEMORY=300 始终 ≥ 此值，避免「内存裁掉但还想落盘」的矛盾。
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

/** 乐观消息的"宽限期" */
const OPTIMISTIC_MSG_GRACE_MS = 30_000

/** 判断本地独有消息（API 未返回的）是否落在「刚生成、后端可能尚未持久化」的宽限期内。
 *
 *  仅 user 乐观消息走宽限期（带 clientMessageId，后端回传同一 ID 后可对账）。
 *  assistant 消息不走宽限期：initFromAPI 是全量权威对账，应信任 API；
 *  正在 streaming 的 assistant 已由 isStreamingMessage 保护，无需时间窗口兜底。
 *  （去掉宽限期可防止 ensureStreamingPlaceholder 合并覆盖 id 后，本地气泡
 *   id ≠ API record_id 导致的刷新后 AI 回复重复渲染。） */
function isWithinOptimisticGrace(m: Message): boolean {
  if (m.role === 'user' && m.clientMessageId) {
    return Date.now() - new Date(m.timestamp).getTime() < OPTIMISTIC_MSG_GRACE_MS
  }
  return false
}

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
 *
 * 注意：仅影响落盘数据，内存中的 messagesByPipeline 不受影响；
 * 被淘汰管道刷新后由 API 冷启动重新加载。
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

/** 单个管道在「内存」中保留的最大消息条数 与 PERSIST_MAX_MESSAGES_PER_PIPELINE（仅持久化裁剪）不同：内存里的 */
const MAX_MESSAGES_PER_PIPELINE_IN_MEMORY = 2000

/** 限制单管道内存消息数，防止无限增长导致浏览器 OOM。 仅在超量时裁剪：按 sequence 排序后保留最新的 N 条。未超限时只做一次 */
function capMessagesForMemory(msgs: Message[]): Message[] {
  if (msgs.length <= MAX_MESSAGES_PER_PIPELINE_IN_MEMORY) return msgs
  return [...msgs].sort(compareMessages).slice(-MAX_MESSAGES_PER_PIPELINE_IN_MEMORY)
}

/** 并发去重：跟踪正在进行的 fetch 请求，避免同一 pipelineId 重复请求 */
const _fetchingPipelines = new Map<string, Promise<void>>()

/** 管道元数据 */
export interface PipelineMeta {
  /** 管道唯一标识 */
  pipelineId: string
  /** 所属会话 ID */
  sessionId: string
  /** 管道层级：1=主管道，2=子管道，3=孙管道 */
  level: 1 | 2 | 3
  /** 关联的 Tab ID（主管道为 null） */
  tabId: string | null
  /** Agent 名称 */
  agentName: string
  /** 管道状态 */
  status: 'idle' | 'running' | 'completed' | 'error'
  /** 父管道 ID（主管道为 null） */
  parentId: string | null
  /** 未读消息计数 */
  unreadCount: number
}

/** 单个管道的流式状态 */
export interface StreamingStatus {
  /** 是否正在流式传输 */
  isStreaming: boolean
  /** 正在流式传输的消息 ID */
  messageId: string | null
}

/** Store 状态接口 */
interface PipelineMessageState {
  /** 一级索引：pipelineId → 消息列表 */
  messagesByPipeline: Record<string, Message[]>
  /** 管道元数据 */
  pipelines: Record<string, PipelineMeta>
  /** 管道归属映射：pipelineId → sessionId */
  pipelineSessionMap: Record<string, string>
  /** 流式状态 */
  streamingState: Record<string, StreamingStatus>
  /** 当前激活的管道 ID */
  activePipelineId: string | null
  /** 顶部游标：pipelineId → 已加载的最小 sequence（用于向上翻页） */
  topCursorsByPipeline: Record<string, number>
  /** 底部游标：pipelineId → 已确认的最大 sequence（用于断线补漏） */
  bottomCursorsByPipeline: Record<string, number>
  /** 是否还有更早的消息：pipelineId → boolean */
  hasMoreOlderByPipeline: Record<string, boolean>
  /** 是否正在加载更早的消息 */
  isLoadingOlderByPipeline: Record<string, boolean>
  /**
   * 累计向上翻页插入的条数：pipelineId → number。
   * 用于驱动虚拟列表的 firstItemIndex（prepend 时递减以保持视口位置）。
   * initFromAPI 全量重建时重置为 0；prependMessages 成功插入 N 条时 +N。
   * 由数据源（store）权威维护，组件只读取，避免在组件内对比前后帧猜测。
   */
  prependedCountByPipeline: Record<string, number>
  /** 运行时标记：本次会话已与后端全量对账过的 pipeline（不持久化，rehydrate 后重置）。
   *  防止流式断线残留的不可信 bottomCursor 导致刷新后只走增量补漏、已加载区间内空洞永远补不上。 */
  reconciledByPipeline: Record<string, boolean>

  /** 注册管道 */
  registerPipeline: (meta: PipelineMeta) => void
  /** 激活管道 */
  activatePipeline: (pipelineId: string) => void

  /** 添加消息到指定管道 */
  addMessage: (pipelineId: string, message: Message) => void
  /** 更新指定管道中的消息（部分更新） */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => void
  /** 移除指定管道中的消息 */
  removeMessage: (pipelineId: string, messageId: string) => void
  /** 获取指定管道的消息列表 */
  getMessages: (pipelineId: string) => Message[]

  /** 开始流式传输 */
  startStreaming: (pipelineId: string, messageId: string) => void
  /** 停止流式传输 */
  stopStreaming: (pipelineId: string) => void
  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => boolean

  /** 冷启动：从 API 写入最新消息并设置双游标 */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => void
  /** 向上翻页：将更早消息插入头部并更新 topCursor */
  prependMessages: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => void
  /** 断线补漏：追加缺失消息到底部并更新 bottomCursor */
  appendMessages: (pipelineId: string, messages: Message[]) => void
  /** 获取指定管道的顶部游标 */
  getTopCursor: (pipelineId: string) => number
  /** 获取指定管道的底部游标 */
  getBottomCursor: (pipelineId: string) => number
  /** 判断指定管道是否已初始化 */
  isInitialized: (pipelineId: string) => boolean
  /** 判断指定管道是否还有更早的消息 */
  hasMoreOlder: (pipelineId: string) => boolean
  /** 获取指定管道累计向上翻页插入的条数（驱动虚拟列表 firstItemIndex） */
  getPrependedCount: (pipelineId: string) => number

  /** 直接从 API 加载指定管道的历史消息（底层，吞异常已修复，调用方应优先用 loadPipelineMessages） */
  fetchMessages: (
    pipelineId: string,
    options?: { limit?: number; before_sequence?: number; after_sequence?: number; threadId?: string },
  ) => Promise<void>
  /** 加载管道消息的统一入口（收敛所有加载场景的流式保护、双游标决策）。 4 个加载场景（会话切换 / 子 Tab 切换 / 关 Tab 回主 / WS 重连补漏）都应调用本方法， */
  loadPipelineMessages: (
    pipelineId: string,
    options: {
      threadId: string
      mode?: 'auto' | 'init' | 'backfill'
      skipStreamingCheck?: boolean
    },
  ) => Promise<{ ok: boolean; error?: unknown }>

  // Parts 统一修改方法

  /** 追加一个新 Part 到指定消息 */
  appendPart: (pipelineId: string, messageId: string, part: MessagePart) => void
  /** 更新指定消息的某个 Part（按 partIndex 精确定位） */
  updatePart: (pipelineId: string, messageId: string, partIndex: number, updates: Partial<MessagePart>) => void
  /** 向指定 Part 追加文本内容（用于流式增量） */
  appendToPart: (pipelineId: string, messageId: string, partIndex: number, content: string) => void
  /** 结束消息流式状态：所有 Part.state = 'done', 消息 status = 'completed' */
  finalizeMessage: (pipelineId: string, messageId: string) => void
  /** 获取指定消息中最后一个指定类型的 Part 的 index */
  findLastPartIndex: (pipelineId: string, messageId: string, type: MessagePart['type']) => number
  /** 获取指定消息中 state='streaming' 的最后一个 Part 的 index */
  findStreamingPartIndex: (pipelineId: string, messageId: string) => number
  /** 获取指定消息中指定 callId 的 tool_call Part 的 index */
  findToolCallPartIndex: (pipelineId: string, messageId: string, callId: string) => number
}

/**
 * 过滤完全空白的 assistant 消息（无 content、无 parts、非 streaming）。
 * 这些消息来自后端记录但不包含可渲染内容，渲染为空气泡。
 */
function filterBlankMessages(messages: Message[]): Message[] {
  return messages.filter((m) => {
    if (m.role !== 'assistant') return true
    if (m.status === 'streaming') return true
    const hasContent = m.content && m.content.trim()
    const hasParts = m.parts && m.parts.length > 0
    return hasContent || hasParts
  })
}

/** 判断消息是否处于流式状态（不可参与合并） */
function isStreamingMessage(msg: Message): boolean {
  if (msg.role !== 'assistant') return false
  if (msg.status === 'streaming') return true
  const parts = msg.parts as MessagePart[] | undefined
  if (parts && parts.length > 0) {
    return parts.some((p) => {
      const state = (p as { state?: string }).state
      return state === 'streaming' || state === 'calling'
    })
  }
  return false
}

/** 合并连续 assistant 消息，用对话结构边界（user/system）和流式状态共同分割。 分隔符（硬边界）：user/system 消息——它们是 AI 消息之间的天然结构边界， */
function mergePreservingStreaming(messages: Message[]): Message[] {
  if (messages.length <= 1) return messages
  const result: Message[] = []
  let segment: Message[] = []
  const flush = () => {
    if (segment.length > 0) {
      const merged = mergeConsecutiveAssistantMessages(segment)
      for (const m of merged) result.push(m)
      segment = []
    }
  }
  for (const msg of messages) {
    // user/system 是对话结构边界：AI 不能跨过它们与另一段 AI 合并
    // （否则系统通知消失后，前后 AI 气泡被错误合并成一条）。
    // 流式 assistant 状态未定，也不能进 segment 被合并。
    if (msg.role === 'user' || msg.role === 'system' || isStreamingMessage(msg)) {
      flush()
      result.push(msg)
    } else {
      segment.push(msg)
    }
  }
  flush()
  return result
}

/** 排序键优先级：sequence → timestamp → id（确保 sequence/timestamp 相同时排序稳定）。 */
function compareMessages(a: Message, b: Message): number {
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

/** 合并两个已排序数组，返回新的已排序数组 */
function mergeSorted(a: Message[], b: Message[]): Message[] {
  const result: Message[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (compareMessages(a[i], b[j]) <= 0) {
      result.push(a[i++])
    } else {
      result.push(b[j++])
    }
  }
  while (i < a.length) result.push(a[i++])
  while (j < b.length) result.push(b[j++])
  return result
}

/** 生成消息指纹，用于跨 ID 格式（WS UUID vs API hex）去重 */
function makeMessageFingerprint(m: Message): string {
  const seq = m.sequence
  if (seq != null) {
    return m.role + '::seq::' + seq
  }
  // Fallback: include content prefix for disambiguation
  const contentPrefix = (m.content || '').substring(0, 80)
  return m.role + '::' + m.timestamp + '::' + contentPrefix
}

/**
 * 判断本地消息是否被 API 权威消息覆盖（即二者是同一条逻辑消息）。
 *
 * 去重规则唯一真相源，全量对账（initFromAPI）与增量补漏（append/prepend）共用：
 * - id 相同 → 同一条（后端 record_id == WS message_id，正常路径）
 * - clientMessageId 相同 → 同一条（user 乐观版 id=前端 UUID，API 版 id=后端
 *   record_id，id 不同但后端从乐观消息回传了相同 clientMessageId）
 *
 * 命中时本地版让位 API 版（丢弃本地、保留 API），保证全量与增量两条路径的
 * 渲染终态一致 —— 切会话回来（增量）与刷新（全量）不会产生不同的消息列表。
 */
function isCoveredByApi(m: Message, apiIds: Set<string>, apiByClientId: Map<string, Message>): boolean {
  if (apiIds.has(m.id)) return true
  if (m.clientMessageId && apiByClientId.has(m.clientMessageId)) return true
  return false
}

/** 合并 API 权威消息与本地已有消息 策略： */
function mergeApiWithExisting(
  sorted: Message[],
  existing: Message[] | undefined,
): { finalMessages: Message[]; preservedCount: number } {
  if (!existing || existing.length === 0) {
    return { finalMessages: sorted, preservedCount: 0 }
  }

  const apiIds = new Set(sorted.map((m) => m.id))
  const apiByClientId = new Map<string, Message>()
  for (const m of sorted) {
    if (m.clientMessageId) {
      apiByClientId.set(m.clientMessageId, m)
    }
  }

  // 本地独有的消息（API 没有的）保留策略：
  // 1. 正在 streaming 的占位消息 — 必须保留（等 stream_end/new_message 收尾）
  // 2. 刚发送的乐观 user 消息（30s 窗口内，带 clientMessageId） — 保留（后端可能尚未持久化）
  // 3. 其余本地消息 — 丢弃，以 API 权威数据为准。
  const localOnly = existing.filter((m) => {
    if (isCoveredByApi(m, apiIds, apiByClientId)) return false
    // 系统通知是 AI 消息之间的结构分隔符，必须保留：
    // 后端 system_notification 是瞬态事件，通常不在消息历史 API 中返回，
    // 若丢弃则刷新/切 Tab 后它消失，前后的 AI 气泡失去边界被合并成一条。
    if (m.role === 'system') return true
    // 正在 streaming 的占位消息必须保留
    if (isStreamingMessage(m)) return true
    // 乐观/刚完成的消息在持久化窗口内保留（后端可能尚未写入）
    if (isWithinOptimisticGrace(m)) return true
    // 其余本地消息：API 没有就以 API 为准丢弃
    return false
  })

  if (localOnly.length === 0) {
    return { finalMessages: sorted, preservedCount: 0 }
  }

  // localOnly 保留的消息若与 API 同 role::seq 指纹，视为同一条逻辑消息，
  // 从 sorted 移除 API 重复项，保留 localOnly 版本（流式占位符 / 乐观版本）。
  // // streaming 占位符与 API 权威消息 id 不同（WS UUID vs API hex），靠 role::seq
  // 指纹识别为同一条，避免切换 Tab 时 AI 气泡重复渲染。
  // // 宽限期保留的 completed assistant 消息同理——后端已持久化时 API 会返回同 seq
  // 权威版本，此时去重只留一份，避免「保留乐观版 + API 版」并存成两条。
  // 后端未持久化时 API 不含该指纹，localOnly 版本正常保留，等待下次 fetch 对账。
  const localOnlyFingerprints = new Set(localOnly.map((m) => makeMessageFingerprint(m)))
  const dedupedSorted = localOnlyFingerprints.size
    ? sorted.filter((m) => !localOnlyFingerprints.has(makeMessageFingerprint(m)))
    : sorted

  // 注意：mergeSorted 要求两个输入各自升序，localOnly 来自 existing（可能无序，
  // 如 persist 恢复或并发写入），需先排序。
  const sortedLocalOnly = [...localOnly].sort(compareMessages)
  return { finalMessages: mergeSorted(dedupedSorted, sortedLocalOnly), preservedCount: localOnly.length }
}

/**
 * 增量补漏（append/prepend）合并：API 仅返回新增消息（after_sequence 增量或
 * before_sequence 翻页），本地已有历史必须全部保留 —— 与 initFromAPI 的全量对账
 * 不同（全量会丢弃 API 没返回的旧消息，增量必须保留 ≤ bottomCursor 的历史）。
 *
 * 去重规则与全量路径共用 isCoveredByApi：本地消息若被 API 覆盖（同 id 或同
 * clientMessageId），让位 API 版，保证增量与全量渲染终态一致。user 乐观版
 * （id=前端 UUID）与 API 版（id=后端 record_id）同 clientMessageId 时不会并存。
 *
 * 仅做合并 + 去重，后处理（流式合并 / 空气泡过滤 / 内存封顶）由调用方按需追加。
 */
function mergeIncrementalApiWithLocal(apiSorted: Message[], existing: Message[]): Message[] {
  if (apiSorted.length === 0) return existing
  if (existing.length === 0) return apiSorted

  const apiIds = new Set(apiSorted.map((m) => m.id))
  const apiByClientId = new Map<string, Message>()
  for (const m of apiSorted) {
    if (m.clientMessageId) apiByClientId.set(m.clientMessageId, m)
  }

  // 本地消息：被 API 覆盖 → 让位 API 版（丢弃本地乐观版）；其余全部保留（增量语义）。
  const keptLocal = existing.filter((m) => !isCoveredByApi(m, apiIds, apiByClientId))

  // mergeSorted 要求两边各自升序；keptLocal 来自 existing（可能无序），先排序。
  return mergeSorted([...keptLocal].sort(compareMessages), apiSorted)
}

/** 计算 bottom 游标（只增不减，防止流式消息 sequence 临时值导致回退） 取 max(API 返回的最大 seq, 现有 bottomCursor)，只增不减。 */
function calculateBottomCursor(finalMessages: Message[], existingCursor: number | undefined): number {
  const apiBottomCursor = finalMessages.length > 0
    ? finalMessages.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
    : 0
  return Math.max(apiBottomCursor, existingCursor ?? 0)
}


/** 统一管道消息 Store */
export const usePipelineMessageStore = create<PipelineMessageState>()(
  persist((set, get) => ({
  messagesByPipeline: {},
  pipelines: {},
  pipelineSessionMap: {},
  streamingState: {},
  activePipelineId: null,
  topCursorsByPipeline: {},
  bottomCursorsByPipeline: {},
  hasMoreOlderByPipeline: {},
  isLoadingOlderByPipeline: {},
  prependedCountByPipeline: {},
  reconciledByPipeline: {},

  /** 注册管道，建立 pipelineId 与元数据的映射 */
  registerPipeline: (meta: PipelineMeta) => {
    set((state) => {
      const existingMeta = state.pipelines[meta.pipelineId]
      // 如果已存在相同 pipelineId，保留已有消息和未读计数
      return {
        pipelines: {
          ...state.pipelines,
          [meta.pipelineId]: existingMeta
            ? { ...existingMeta, ...meta, unreadCount: existingMeta.unreadCount }
            : meta,
        },
        pipelineSessionMap: {
          ...state.pipelineSessionMap,
          [meta.pipelineId]: meta.sessionId,
        },
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [meta.pipelineId]: state.messagesByPipeline[meta.pipelineId] || [],
        },
      }
    })
  },

  /** 激活管道，同时重置该管道的未读计数 */
  activatePipeline: (pipelineId: string) => {
    set((state) => {
      const meta = state.pipelines[pipelineId]
      return {
        activePipelineId: pipelineId,
        pipelines: meta
          ? {
              ...state.pipelines,
              [pipelineId]: { ...meta, unreadCount: 0 },
            }
          : state.pipelines,
      }
    })
  },

  /** 添加消息到指定管道，自动去重和排序 */
  addMessage: (pipelineId: string, message: Message) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const realMessageId = (message as Message & { message_id?: string }).message_id || message.id

      const existingIndex = pipelineMessages.findIndex((m) => m.id === realMessageId)

      let updatedMessages: Message[]
      let unreadChanged = false

      if (existingIndex >= 0) {
        updatedMessages = [...pipelineMessages]
        updatedMessages[existingIndex] = {
          ...pipelineMessages[existingIndex],
          ...message,
          id: pipelineMessages[existingIndex].id,
        }
      } else {
        updatedMessages = [...pipelineMessages, { ...message, id: realMessageId }]
        if (state.activePipelineId !== pipelineId) {
          unreadChanged = true
        }
      }

      const newPipelines = { ...state.pipelines }
      if (unreadChanged && newPipelines[pipelineId]) {
        newPipelines[pipelineId] = {
          ...newPipelines[pipelineId],
          unreadCount: newPipelines[pipelineId].unreadCount + 1,
        }
      }

      // bottomCursor 只由 API 权威路径（initFromAPI / appendMessages / prependMessages）维护。
      // addMessage 是乐观/流式/通知消息的入口（router 乐观 user、ensureStreamingPlaceholder
      // 流式占位、handleSystemNotification 系统通知），其 sequence 来自本地分配
      // （allocateNextSequence 可能用 Math.max(localMax+1) 抬升），并非后端权威值。
      // 若让它推进 bottomCursor，会污染双游标：
      //   - 系统通知 sequence 被抬到 localMax+1，initFromAPI 重排时被 mergeSorted 排到末尾，
      //     导致「通知跑到 AI 回复后面」（排序错乱）。
      //   - 流式占位的临时 sequence 进入游标，下次 after_sequence=bottomCursor 补漏会跳过
      //     真正未加载的权威消息。
      // 流式期间 bottomCursor 保持不动，等流式结束 → 切 Tab/重连触发 appendMessages 时
      // 由 calculateBottomCursor 写入权威值。

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
          [pipelineId]: capMessagesForMemory(updatedMessages),
        },
        pipelines: newPipelines,
      }
    })
  },

  /** 更新指定管道中的消息（部分更新），支持模糊匹配 注意：找不到消息时不会自动创建，仅输出 warn 日志。 */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []

      let messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      // 精确匹配失败时，assistant 消息尝试基于 sequence 模糊匹配
      if (messageIndex < 0 && partial.role === 'assistant' && partial.sequence != null) {
        messageIndex = pipelineMessages.findIndex((m) =>
          m.role === 'assistant' && m.sequence === partial.sequence,
        )
      }

      if (messageIndex < 0) {
        if (partial.sequence != null) {
          const fingerprint = (partial.role || 'assistant') + '::seq::' + partial.sequence
          messageIndex = pipelineMessages.findIndex((m) => makeMessageFingerprint(m) === fingerprint)
        }
      }

      if (messageIndex < 0) {
        logger.error(
          '[updateMessage] 目标消息不存在，跳过更新（不创建避免重复）: pipelineId=%s messageId=%s role=%s seq=%s',
          pipelineId?.slice(0, 12),
          messageId?.slice(0, 12),
          partial.role ?? 'unknown',
          partial.sequence ?? 'unknown',
        )
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        ...partial,
        _lastUpdated: Date.now(),
      } as Message

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 获取指定管道的消息列表 */
  getMessages: (pipelineId: string) => {
    return get().messagesByPipeline[pipelineId] || []
  },

  /** 移除指定管道中的消息 */
  removeMessage: (pipelineId: string, messageId: string) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (messageIndex < 0) return state
      const updatedMessages = pipelineMessages.filter((_, i) => i !== messageIndex)
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 开始流式传输，记录正在流式传输的消息 ID */
  startStreaming: (pipelineId: string, messageId: string) => {
    set((state) => ({
      streamingState: {
        ...state.streamingState,
        [pipelineId]: {
          isStreaming: true,
          messageId,
          startedAt: Date.now(),
        },
      },
    }))
  },

  /** 停止流式传输，同时将消息状态标记为 completed */
  stopStreaming: (pipelineId: string) => {
    set((state) => {
      const streamStatus = state.streamingState[pipelineId]
      const newStreamingState = { ...state.streamingState }
      delete newStreamingState[pipelineId]

      if (streamStatus?.messageId) {
        const pipelineMessages = state.messagesByPipeline[pipelineId] || []
        const messageIndex = pipelineMessages.findIndex(
          (m) => m.id === streamStatus.messageId,
        )

        if (messageIndex >= 0) {
          const updatedMessages = [...pipelineMessages]
          updatedMessages[messageIndex] = {
            ...updatedMessages[messageIndex],
            status: 'completed',
          }

          return {
            streamingState: newStreamingState,
            messagesByPipeline: {
              ...state.messagesByPipeline,
              [pipelineId]: updatedMessages,
            },
          }
        }
      }

      return {
        streamingState: newStreamingState,
      }
    })
  },

  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => {
    return get().streamingState[pipelineId]?.isStreaming ?? false
  },

  /** 冷启动：从 API 写入最新消息并设置双游标 FIX: 合并策略 — streaming 消息仅在 API 未返回同 ID 时保留，其余以 API 数据为准。 */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      // ★ 刷新语义：完全丢弃本地消息，只用 API 权威数据。
      // 不保留任何 localOnly（不合并、不宽限、不 streaming 保护）——
      // 刷新后所有内容都从后端持久化拿，本地流式缓存一律丢弃。
      // 后端正在输出时，WS 重连的 backfill 增量补漏 + 续流会补回新内容。
      let finalMessages = sorted
      // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
      finalMessages = filterBlankMessages(finalMessages)
      // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
      finalMessages = capMessagesForMemory(finalMessages)

      const topCursor = finalMessages.length > 0 ? (finalMessages[0].sequence ?? 0) : 0
      const bottomCursor = calculateBottomCursor(finalMessages, state.bottomCursorsByPipeline[pipelineId])

      logger.info('[initFromAPI] done: pipelineId=%s finalMsgs=%d (全量替换，不保留本地)',
        pipelineId?.slice(0, 12), finalMessages.length)

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMessages,
        },
        topCursorsByPipeline: {
          ...state.topCursorsByPipeline,
          [pipelineId]: topCursor,
        },
        bottomCursorsByPipeline: {
          ...state.bottomCursorsByPipeline,
          [pipelineId]: bottomCursor,
        },
        hasMoreOlderByPipeline: {
          ...state.hasMoreOlderByPipeline,
          // 后端始终返回 has_more，前端直接使用
          [pipelineId]: hasMoreOlder ?? false,
        },
        // 全量重建：prepended 计数归零（firstItemIndex 回到基准值）
        prependedCountByPipeline: {
          ...state.prependedCountByPipeline,
          [pipelineId]: 0,
        },
      }
    })
  },

  /** 向上翻页：将更早消息插入头部并更新 topCursor */
  prependMessages: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      if (messages.length === 0) {
        return {
          hasMoreOlderByPipeline: {
            ...state.hasMoreOlderByPipeline,
            [pipelineId]: false,
          },
          isLoadingOlderByPipeline: {
            ...state.isLoadingOlderByPipeline,
            [pipelineId]: false,
          },
        }
      }
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      // 含 clientMessageId 对账（与 appendMessages 共用 mergeIncrementalApiWithLocal）。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
        // 跨边界合并保留 streaming 流式片段。
        let finalMerged = mergePreservingStreaming(merged)
        // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
        finalMerged = filterBlankMessages(finalMerged)
        // 注意：prepend 路径不调用 capMessagesForMemory。
        // 用户主动向上翻页是为了「看历史」，裁剪最老消息会破坏视口并导致最新消息
        // 因 sequence 重排而丢失。OOM 防护由虚拟列表（DOM 只渲染可见区）承担；
        // 内存数组增长由 addMessage/init/append 路径的 cap 兜底（非用户主动意图）。
      const topCursor = finalMerged[0]?.sequence ?? 0
      // 净增条数：驱动 firstItemIndex 递减，让虚拟列表 prepend 时保持视口位置。
      // 用 finalMerged - existing 的差值，比 API 返回条数更准（考虑去重）。
      const prependedDelta = Math.max(0, finalMerged.length - existing.length)
      const prevPrepended = state.prependedCountByPipeline[pipelineId] ?? 0
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMerged,
        },
        topCursorsByPipeline: {
          ...state.topCursorsByPipeline,
          [pipelineId]: topCursor,
        },
        hasMoreOlderByPipeline: {
          ...state.hasMoreOlderByPipeline,
          // 后端始终返回 has_more，前端直接使用
          [pipelineId]: hasMoreOlder ?? false,
        },
        isLoadingOlderByPipeline: {
          ...state.isLoadingOlderByPipeline,
          [pipelineId]: false,
        },
        prependedCountByPipeline: {
          ...state.prependedCountByPipeline,
          [pipelineId]: prevPrepended + prependedDelta,
        },
      }
    })
  },

  /** 断线补漏：追加缺失消息到底部并更新 bottomCursor */
  appendMessages: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      if (messages.length === 0) return state
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      // 含 clientMessageId 对账：user 乐观版（UUID id）与 API 版（record_id）
      // 同 clientMessageId 时丢弃本地乐观版，避免切会话回来两条 user 并存。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
      // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
      const finalMerged = capMessagesForMemory(merged)
      const bottomCursor = finalMerged.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: finalMerged,
        },
        bottomCursorsByPipeline: {
          ...state.bottomCursorsByPipeline,
          [pipelineId]: bottomCursor,
        },
      }
    })
  },

  /** 获取指定管道的顶部游标 */
  getTopCursor: (pipelineId: string) => {
    return get().topCursorsByPipeline[pipelineId] ?? 0
  },

  /** 获取指定管道的底部游标 */
  getBottomCursor: (pipelineId: string) => {
    return get().bottomCursorsByPipeline[pipelineId] ?? 0
  },

  /** 判断指定管道是否已成功加载过消息（可走增量补漏而非全量）。 权威定义：bottomCursor>0（已确认最大 sequence）且已有 >1 条消息。 */
  isInitialized: (pipelineId: string) => {
    const state = get()
    const count = (state.messagesByPipeline[pipelineId] || []).length
    const bottomCursor = state.bottomCursorsByPipeline[pipelineId] ?? 0
    return bottomCursor > 0 && count > 1
  },

  /** 判断指定管道是否还有更早的消息 */
  hasMoreOlder: (pipelineId: string) => {
    return get().hasMoreOlderByPipeline[pipelineId] ?? false
  },

  /** 获取指定管道累计向上翻页插入的条数（驱动虚拟列表 firstItemIndex） */
  getPrependedCount: (pipelineId: string) => {
    return get().prependedCountByPipeline[pipelineId] ?? 0
  },

  /** 将旧管道中最近的用户消息迁移到新管道 */
  fetchMessages: async (
    pipelineId: string,
    options?: { limit?: number; before_sequence?: number; after_sequence?: number; threadId?: string },
  ) => {
    if (pipelineId.startsWith('temp-')) {
      get().initFromAPI(pipelineId, [])
      return
    }

    // 并发去重：按方向区分 key，避免向上翻页和向下补漏互相阻塞
    const dedupeKey = options?.before_sequence !== undefined
      ? `${pipelineId}::older`
      : options?.after_sequence !== undefined
        ? `${pipelineId}::newer`
        : `${pipelineId}::init`
    const existingFetch = _fetchingPipelines.get(dedupeKey)
    if (existingFetch) {
      return existingFetch
    }
    // 防御：older 请求不得与 init 并发。init 是全量替换（initFromAPI），older 是增量
    // prepend，二者去重 key 不同互不阻塞，若并发会导致 prepend 的历史被 init 全量覆盖
    // 丢失或重复加载。merge 已在刷新时清空 hasMoreOlder 拦截大部分情况，这里再加一层：
    // init 进行中时直接拒绝 older，等 init 返回重设 hasMoreOlder 后再放行。
    if (options?.before_sequence !== undefined && _fetchingPipelines.has(`${pipelineId}::init`)) {
      return
    }

    const fetchPromise = (async () => {
      // 加载更早消息时，先设置 loading 状态（防重复请求 + 显示加载指示器）
      if (options?.before_sequence !== undefined) {
        set((state) => ({
          isLoadingOlderByPipeline: {
            ...state.isLoadingOlderByPipeline,
            [pipelineId]: true,
          },
        }))
      }
      try {
        const limit = options?.limit ?? 50
        // 自动从 pipelineSessionMap 查找 sessionId 作为 threadId fallback
        // FEATURE-pipeline_unify: 统一传 pipelineRunId（主/子管道都用 pipelineId），
        // 后端统一走 pipelineRunId 路径，不再区分主/子。
        const sessionFallback = get().pipelineSessionMap[pipelineId]
        // 内部 API 不需要 threadId 三层降级：优先传参，其次 pipelineSessionMap，
        // 二者都无说明调用链有问题，直接报错
        const threadId = options?.threadId || sessionFallback
        if (!threadId) {
          logger.error('[pipelineMessageStore.fetchMessages] 无法确定 threadId: pipelineId=%s', pipelineId)
          throw new Error(`无法确定 threadId，pipelineId: ${pipelineId}`)
        }
        // 内部 API 不做内置重试，429/5xx 由 axios interceptor 统一处理
        const apiResult = await apiGetMessages(threadId, {
          limit,
          before_sequence: options?.before_sequence,
          after_sequence: options?.after_sequence,
          pipelineRunId: pipelineId,
        })

        const rawMessages: Message[] = apiResult.messages || []
        // 后端 MessageQueryBuilder 已确保只返回当前版本消息，前端无需按 parentId 过滤。
        const mainMessages = rawMessages

        if (options?.after_sequence !== undefined) {
          get().appendMessages(pipelineId, mainMessages)
        } else if (options?.before_sequence !== undefined) {
          const hasMoreOlder = (apiResult as any)?.has_more ?? false
          get().prependMessages(pipelineId, mainMessages, hasMoreOlder)
        } else {
          // 首次冷启动：从 API 响应读取 has_more，避免首次返回 <50 条时被错误地标记为无更多历史消息
          const hasMoreOlder = (apiResult as any)?.has_more ?? false
          get().initFromAPI(pipelineId, mainMessages, hasMoreOlder)
        }
      } catch (err: any) {
        const status = err?.response?.status ?? err?.status
        if (status === 404) {
          logger.debug('[pipelineMessageStore.fetchMessages] 管道消息暂不可用 (404): pipelineId=%s', pipelineId)
        } else {
          // 提取可读错误标识，避免 AxiosError 对象被 %s 序列化成 [object Object]。
          // 优先级：HTTP 状态码 > axios error code（ECONNABORTED/ERR_NETWORK 等）> message
          const errInfo = status || err?.code || err?.message || String(err)
          logger.warn(
            '[pipelineMessageStore.fetchMessages] 加载失败（已重试）: pipelineId=%s err=%s',
            pipelineId, errInfo,
          )
        }
        // 重新抛出，让上层调用方（loadPipelineMessages）能感知失败并决定通知策略。
        // 不在此处吞异常，否则所有调用方的 catch/then 分支永远拿不到错误。
        throw err
      } finally {
        _fetchingPipelines.delete(dedupeKey)
        // 导致用户滚动到顶部后"加载更多"完全失效。
        if (options?.before_sequence !== undefined) {
          set((state) => {
            if (state.isLoadingOlderByPipeline[pipelineId]) {
              return {
                isLoadingOlderByPipeline: {
                  ...state.isLoadingOlderByPipeline,
                  [pipelineId]: false,
                },
              }
            }
            return state
          })
        }
      }
    })()

    // 记录正在进行的请求
    _fetchingPipelines.set(dedupeKey, fetchPromise)

    return fetchPromise
  },

  /** 加载管道消息的统一入口。收敛所有加载场景的流式保护 + 双游标决策。 详见接口声明处的注释。 */
  loadPipelineMessages: async (pipelineId, options) => {
    const { threadId, mode = 'auto', skipStreamingCheck = false } = options
    const state = get()
    const existingCount = (state.messagesByPipeline[pipelineId] || []).length

    // 流式保护：流式输出中且已有实质消息时跳过所有 API 调用。
    // 切换会话：显示当前流式进度，不覆盖。
    // WS 重连场景传 skipStreamingCheck=true 强制加载。
    if (!skipStreamingCheck && state.isStreaming(pipelineId) && existingCount > 1) {
      return { ok: true }
    }

    // 模式决策：
    // - mode='init'：页面刷新 / 显式强制 → 全量 initFromAPI，丢弃本地，以 API 权威重建
    // - mode='auto'：切换会话 → 该管道尚未对账（刷新后首次进入 / 无缓存）：全量对账一次；
    //   已对账：不做任何 API 调用，直接用缓存
    // - mode='backfill'：WS 重连补漏 → 增量追加
    const reconciled = state.reconciledByPipeline[pipelineId] ?? false
    const isInit = mode === 'init' || (mode === 'auto' && !reconciled)
    const needBackfill = mode === 'backfill'

    try {
      if (isInit) {
        await state.fetchMessages(pipelineId, { threadId })
        set((s) => ({ reconciledByPipeline: { ...s.reconciledByPipeline, [pipelineId]: true } }))
      } else if (needBackfill) {
        const bottomCursor = state.bottomCursorsByPipeline[pipelineId] ?? 0
        await state.fetchMessages(pipelineId, { threadId, after_sequence: bottomCursor })
      }
      // mode='auto' 且已对账 或 缓存为空 → 不做 API 调用，直接用缓存
      return { ok: true }
    } catch (error) {
      return { ok: false, error }
    }
  },

  // Parts 统一修改方法

  /** 追加一个新 Part 到指定消息 */
  appendPart: (pipelineId: string, messageId: string, part: MessagePart) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId]
      if (!pipelineMessages) return state
      const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (msgIndex < 0) return state
      const msg = pipelineMessages[msgIndex]
      const updatedMessages = [...pipelineMessages]
      updatedMessages[msgIndex] = {
        ...msg,
        parts: [...(msg.parts || []), part],
        _lastUpdated: Date.now(),
      }
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 更新指定消息的某个 Part（按 partIndex 精确定位） */
  updatePart: (pipelineId: string, messageId: string, partIndex: number, updates: Partial<MessagePart>) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId]
      if (!pipelineMessages) return state
      const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (msgIndex < 0) return state
      const msg = pipelineMessages[msgIndex]
      const parts = msg.parts || []
      if (partIndex < 0 || partIndex >= parts.length) return state
      const updatedParts = [...parts]
      updatedParts[partIndex] = { ...updatedParts[partIndex], ...updates } as MessagePart
      const updatedMessages = [...pipelineMessages]
      updatedMessages[msgIndex] = {
        ...msg,
        parts: updatedParts,
        _lastUpdated: Date.now(),
      }
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 向指定 Part 追加文本内容（用于流式增量） */
  appendToPart: (pipelineId: string, messageId: string, partIndex: number, content: string) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId]
      if (!pipelineMessages) return state
      const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (msgIndex < 0) return state
      const msg = pipelineMessages[msgIndex]
      const parts = msg.parts || []
      if (partIndex < 0 || partIndex >= parts.length) return state
      const part = parts[partIndex]
      // 只有 text 和 thinking 类型支持追加
      if (part.type !== 'text' && part.type !== 'thinking') return state
      const updatedParts = [...parts]
      updatedParts[partIndex] = {
        ...part,
        content: (part as { content: string }).content + content,
      } as MessagePart
      const updatedMessages = [...pipelineMessages]
      updatedMessages[msgIndex] = {
        ...msg,
        parts: updatedParts,
        _lastUpdated: Date.now(),
      }
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 结束消息流式状态：所有 Part.state = 'done', 消息 status = 'completed' */
  finalizeMessage: (pipelineId: string, messageId: string) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId]
      if (!pipelineMessages) return state
      const msgIndex = pipelineMessages.findIndex((m) => m.id === messageId)
      if (msgIndex < 0) return state
      const msg = pipelineMessages[msgIndex]
      const parts = msg.parts || []
      const finalizedParts = parts.map((p) => {
        if (p.type === 'text' || p.type === 'thinking') {
          return { ...p, state: 'done' as const } as MessagePart
        }
        if (p.type === 'tool_call') {
          return {
            ...p,
            state: (p.state === 'error' ? 'error' : 'done') as ('done' | 'error'),
          } as MessagePart
        }
        return p
      })
      const updatedMessages = [...pipelineMessages]
      updatedMessages[msgIndex] = {
        ...msg,
        parts: finalizedParts,
        status: 'completed',
        _lastUpdated: Date.now(),
      }
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })
  },

  /** 获取指定消息中最后一个指定类型的 Part 的 index */
  findLastPartIndex: (pipelineId: string, messageId: string, type: MessagePart['type']) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    for (let i = msg.parts.length - 1; i >= 0; i--) {
      if (msg.parts[i].type === type) return i
    }
    return -1
  },

  /** 获取指定消息中 state='streaming' 的最后一个 text Part 的 index */
  findStreamingPartIndex: (pipelineId: string, messageId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    for (let i = msg.parts.length - 1; i >= 0; i--) {
      const p = msg.parts[i]
      // 仅匹配 text part：正文 stream_chunk 只应追加到 text part。
      // 若匹配 thinking part，后端 </think> 关闭时先发正文 chunk（thinking_end
      // 在下一次 delta 才发）会导致正文被吞进 thinking part，正文不显示或
      // 等 thinking_end 后才一次性渲染。thinking 的流式追加由 thinkingHandler
      // 用 findLastPartIndex(type='thinking') 精确路由，不走本方法。
      if (p.type === 'text' && p.state === 'streaming') return i
    }
    return -1
  },

  /** 获取指定消息中指定 callId 的 tool_call Part 的 index */
  findToolCallPartIndex: (pipelineId: string, messageId: string, callId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    return msg.parts.findIndex((p) => p.type === 'tool_call' && (p as ToolCallPart).callId === callId)
  },
}),
  // 持久化策略（迁移到 IndexedDB 后）：
  // - 单管道最多 250 条（PERSIST_MAX_MESSAGES_PER_PIPELINE）
  // - 全局总体积上限 100 MB（PERSIST_MAX_TOTAL_BYTES），超限按 LRU 淘汰最不活跃管道
  // - 无 TTL：被淘汰或缺失的管道由 API 冷启动重新加载覆盖
  {
    name: 'pipeline-messages',
    version: 1,
    // IndexedDB storage：GB 级容量、异步不阻塞 UI；不可用时自动降级内存（见 indexedDbStorage）
    storage: indexedDbStorage,
    // 仅持久化核心数据，排除运行时状态。
    // 先按条数 + 总体积 LRU 裁剪出保留的管道集合，再统一应用于消息/元数据/游标，保证一致性。
    partialize: (state) => {
      const keptMessages = trimMessagesForPersistence(
        state.messagesByPipeline,
        state.activePipelineId,
      )
      const keptPids = new Set(Object.keys(keptMessages))
      const pickByKey = <V>(rec: Record<string, V>): Record<string, V> => {
        if (!rec) return {}
        const out: Record<string, V> = {}
        for (const [k, v] of Object.entries(rec)) {
          if (keptPids.has(k)) out[k] = v
        }
        return out
      }
      return {
        messagesByPipeline: keptMessages,
        pipelines: pickByKey(state.pipelines),
        pipelineSessionMap: pickByKey(state.pipelineSessionMap),
        activePipelineId: state.activePipelineId,
        topCursorsByPipeline: pickByKey(state.topCursorsByPipeline),
        bottomCursorsByPipeline: pickByKey(state.bottomCursorsByPipeline),
        hasMoreOlderByPipeline: pickByKey(state.hasMoreOlderByPipeline),
      }
    },
    // 恢复时合并默认状态（运行时状态用默认值）
    merge: (persisted, current) => {
      const p = (persisted as Partial<PipelineMessageState>) || {}
      // 近期 streaming 消息宽限期（5 分钟）：
      // 刷新时后端可能还在输出 → 保留 streaming 状态，让 initFromAPI 的
      // isStreamingMessage 保护它不被丢弃；WS 重连后续流。
      // 超过宽限期的 orphan streaming 才标记 completed 兜底。
      const STREAMING_GRACE_MS = 5 * 60 * 1000
      const now = Date.now()
      const cleanedMessages: Record<string, Message[]> = {}
      if (p.messagesByPipeline) {
        for (const [pid, msgs] of Object.entries(p.messagesByPipeline)) {
          if (!msgs) continue
          cleanedMessages[pid] = msgs
            .filter((m) => {
              // 丢弃空的乐观占位（断线期间未收到任何内容）：刷新后后端权威内容由
              // initFromAPI 对账拉取，本地空占位只会变空气泡。保留有内容的占位。
              if (m.status === 'streaming' && m.id.startsWith?.('placeholder_')) {
                const hasContent = (m.content || '').length > 0
                  || (m.parts || []).some((part) => part.type !== 'system')
                return hasContent
              }
              return true
            })
            .map((m) => {
              if (m.status === 'streaming') {
                // 近期 streaming → 保留原样（后端可能还在输出，WS 重连会续流）
                if (typeof m._lastUpdated === 'number' && (now - m._lastUpdated) < STREAMING_GRACE_MS) {
                  return m
                }
                // 过期 orphan streaming → 标记 completed 兜底
                const cleanedParts = (m.parts || []).map((part) => {
                  const state = (part as { state?: string }).state
                  if (state === 'streaming' || state === 'calling') {
                    return { ...part, state: 'done' } as MessagePart
                  }
                  return part
                })
                return { ...m, status: 'completed' as const, parts: cleanedParts }
              }
              return m
            })
        }
      }
      return {
        ...current,
        ...p,
        messagesByPipeline: cleanedMessages,
        // 运行时状态强制重置（不信任持久化值）
        streamingState: {},
        isLoadingOlderByPipeline: {},
        // 重置对账标记：应用重启后持久化的 bottomCursor 不可信（可能来自流式断线时的乐观值），
        // 所有 pipeline 必须重新全量对账，避免已加载区间内的空洞无法通过增量补漏修复。
        reconciledByPipeline: {},
        // 分页游标与 hasMore 同样重置：reconciledByPipeline 已清空，意味着每个 pipeline 都会走
        // initFromAPI 全量对账。若保留快照里的 hasMoreOlder=true / 旧游标，刷新后 init 尚未返回时，
        // MessageList 的 increaseViewportBy 会在初始渲染触发 startReached → onLoadMore 看到
        // hasMoreOlder=true 放行（router.tsx）→ 用旧游标发 older 请求，与 init（去重 key 不同）并发，
        // 导致 prepend 的历史被 initFromAPI 全量覆盖丢失或重复加载。统一「刷新=全量重新对账」，
        // 快照仅用于 init 返回前的占位渲染，绝不参与分页决策。initFromAPI 返回后会用 API 的
        // 真实 has_more + 游标重设这两个字段（见 initFromAPI）。
        hasMoreOlderByPipeline: {},
        topCursorsByPipeline: {},
        bottomCursorsByPipeline: {},
      }
    },
    // 迁移配套：消息缓存已迁 IndexedDB，旧 localStorage['pipeline-messages'] 不再读取。
    // rehydrate 完成后一次性清理旧 key，释放 localStorage 空间给 agentTabs 等使用。
    onRehydrateStorage: () => () => {
      try {
        if (window.localStorage.getItem('pipeline-messages') !== null) {
          window.localStorage.removeItem('pipeline-messages')
        }
      } catch {
        // localStorage 不可用时忽略，不影响 rehydrate
      }
    },
  },
))
