/** 统一管道消息状态管理 Store 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { getMessages as apiGetMessages } from '@/services/api/session'
import { loggers } from '@/utils/logger'
import { indexedDbStorage } from '@/utils/indexedDbStorage'
import { useContextKeys } from '@/stores/contextKeysStore'
// retry removed per audit: 内部 API 不应内置重试，429/5xx 重试统一由 axios interceptor 管理
import type { Message } from '@/types/models'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'
import { decideClaim } from '@/streaming/claim'
import { compareMessages } from '@/utils/messageOrder'

const logger = loggers.sessionStore

/**
 * 每个管道持久化的最大消息条数（IndexedDB 容量充裕，250 给单会话充足历史缓存）。
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

/** initFromAPI 保留「飞行中」本地消息的新鲜度窗口（ms）：乐观 user 以 timestamp、
 *  assistant（流式/刚完成）以 _lastUpdated 判定；超窗的视为 persist 残留/断线
 *  残影，按刷新去漂移语义丢弃。窗口需覆盖后端 init 慢读（大会话全量读可达
 *  数十秒），迟到快照才不会抹掉窗口内的本页活动。 */
const INFLIGHT_FRESH_MS = 90_000

/** 限制单管道内存消息数，防止无限增长导致浏览器 OOM。 仅在超量时裁剪：按 sequence 排序后保留最新的 N 条。未超限时只做一次 */
function capMessagesForMemory(msgs: Message[]): Message[] {
  if (msgs.length <= MAX_MESSAGES_PER_PIPELINE_IN_MEMORY) return msgs
  return [...msgs].sort(compareMessages).slice(-MAX_MESSAGES_PER_PIPELINE_IN_MEMORY)
}

/** 并发去重：跟踪正在进行的 fetch 请求，避免同一 pipelineId 重复请求 */
const _fetchingPipelines = new Map<string, Promise<void>>()

/** 刷新后后台全量对账去重（同一管道只对账一次；流式结束前的对账推迟） */
const _reconcilingPipelines = new Set<string>()

/**
 * 刷新后后台静默全量对账（auto 首次进入且本地有 IndexedDB 缓存时，
 * 页面立即用缓存渲染（秒开），全量 API 对账放后台执行——initFromAPI 权威替换
 * 能修正刷新前流式断线留下的空洞/残影（增量补漏 after_sequence 拉不到已加载
 * 区间内的缺失消息）。对账无变化时对 UI 零影响；失败静默（缓存已渲染，下次
 * 进入/WS 重连重试）。流式进行中跳过——init 全量替换会清流式占位。
 */
async function reconcileFromAPI(pipelineId: string, threadId: string): Promise<void> {
  if (_reconcilingPipelines.has(pipelineId)) return
  const store = usePipelineMessageStore.getState()
  if (store.isStreaming(pipelineId)) return
  _reconcilingPipelines.add(pipelineId)
  try {
    await store.fetchMessages(pipelineId, { threadId })
    usePipelineMessageStore.setState((s) => ({
      reconciledByPipeline: { ...s.reconciledByPipeline, [pipelineId]: true },
    }))
  } catch {
    // 静默失败：缓存已渲染，待下次触发重试
  } finally {
    _reconcilingPipelines.delete(pipelineId)
  }
}

/** IndexedDB rehydrate 完成信号（persist onFinishHydration 设置；刷新后首屏
 *  loadPipelineMessages 在 rehydrate 前到达时等待，否则本地缓存不可见 →
 *  auto 判定"未对账"全量 init，IndexedDB 缓存形同虚设） */
let _hydrated = false
const _hydrationWaiters: Array<() => void> = []
const _markHydrated = () => {
  _hydrated = true
  for (const resolve of _hydrationWaiters.splice(0)) resolve()
}
/** 等待 persist rehydrate 完成（已完成立即返回；500ms 兜底防极端情况挂起） */
function waitForMessageHydration(): Promise<void> {
  if (_hydrated) return Promise.resolve()
  return new Promise((resolve) => {
    _hydrationWaiters.push(resolve)
    // 兜底：rehydrate 未触发（存储异常等）也不阻塞加载，缓存恢复退化为全量 API
    window.setTimeout(() => {
      const idx = _hydrationWaiters.indexOf(resolve)
      if (idx !== -1) _hydrationWaiters.splice(idx, 1)
      resolve()
    }, 500)
  })
}

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

  /** 认领 user 消息（[来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]）：
   *  按 cmid 找到乐观 user
   *  消息 → 权威 record_id/seq 记入独立 recordId 字段（UI 寻址 id 永不变），
   *  status='completed'。候选缺失（断线期间确认到达/刷新窗口）→ 以
   *  cmid 为 id 插入权威 user 版——「发送后用户消息消失」结构性不可能。 */
  claimUserMessage: (pipelineId: string, cmid: string, userRecord: {
    id?: string
    content?: unknown
    sequence?: number
    metadata?: Record<string, unknown> | null
  }) => 'upgraded' | 'inserted' | 'skipped'

  /** 无 cmid 注入消息（触发器/任务/HTTP，ADR-2026-08-26）补插 user 气泡：
   *  按 id 幂等（重复事件不双插）；按 sequence 落位——保证触发器消息出现在
   *  assistant 回复之前（与后端消息序一致）。 */
  ensureInjectedUserMessage: (pipelineId: string, userRecord: {
    id?: string
    content?: unknown
    sequence?: number
    metadata?: Record<string, unknown> | null
  }) => void

  /** 确认主数组乐观 user（旧内核无 user_message 回传的兼容路径）：按 cmid 把
   *  status='sending' 标记为 'completed'（后端已持久化；权威 id/seq 由对账补正）。
   *  无候选 → skipped（不插不建——后端记录由对账拉取）。 */
  confirmUserMessage: (pipelineId: string, cmid: string) => boolean

  /** 获取指定管道中最后一条 user 消息（重新生成缺省目标） */
  findLastUserMessageId: (pipelineId: string) => string | null
  /** 乐观截断：保留到目标 user 消息（含）为止，其后消息全部移除（重新生成/回退/编辑重发本地即时反馈） */
  truncateMessagesAfter: (pipelineId: string, userMessageId: string) => void

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

  /** 直接从 API 加载指定管道的历史消息（底层，调用方应优先用 loadPipelineMessages） */
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
 * 过滤完全空白的 assistant 消息（无 content、无 parts、无 tool_call part、无 thinking、非 streaming）。
 * 这些消息来自后端记录但不包含可渲染内容，渲染为空气泡。
 *
 * 注意：assistant 可能 content 为空但有 tool_call part（发起工具调用）或 thinking
 * （纯思考）。子任务管道大量存在这种消息，若只检查 content/parts 会误删，
 * 导致消息丢失。必须检查 tool_call part 和 thinking。
 */
function filterBlankMessages(messages: Message[]): Message[] {
  return messages.filter((m) => {
    if (m.role !== 'assistant') return true
    if (m.status === 'streaming') return true
    const hasContent = m.content && m.content.trim()
    const hasParts = m.parts && m.parts.length > 0
    const hasToolCalls = (m.parts ?? []).some((p) => p.type === 'tool_call')
    const hasThinking = m.thinking && (m.thinking.content || '').trim()
    return hasContent || hasParts || hasToolCalls || hasThinking
  })
}

/** 排序键优先级：sequence → timestamp → id（确保 sequence/timestamp 相同时排序稳定）。
 *  实现与渲染层共用（@/utils/messageOrder），单一真值源。 */
export { compareMessages }

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

/**
 * 判断本地消息是否被 API 权威消息覆盖（即二者是同一条逻辑消息）。
 *
 * 去重规则唯一真相源，全量对账（initFromAPI）与增量补漏（append/prepend）共用：
 * - id 相同 → 同一条（后端 record_id == WS message_id，正常路径）
 * - clientMessageId 相同 → 同一条（user 乐观版 id=前端 UUID，API 版 id=后端
 *   record_id，id 不同但后端从乐观消息回传了相同 clientMessageId）
 * - recordId 相同 → 同一条（双字段范式：本地已认领的 user 消息
 *   UI id 是前端 uuid、API 权威版 id 是后端 record_id，按 recordId 收敛）
 *
 * 命中时本地版让位 API 版（丢弃本地、保留 API），保证全量与增量两条路径的
 * 渲染终态一致 —— 切会话回来（增量）与刷新（全量）不会产生不同的消息列表。
 * （[来源: docs/decisions/2026-08-21-message-idempotency-contract.md] /
 *  [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]：
 *  仅精确键裁决，不做 role::seq 指纹等模糊匹配——
 * 非唯一键，撞号时写错目标/误删，业界流式系统一律事件携带权威 ID 精确匹配。）
 */
function isCoveredByApi(
  m: Message,
  apiIds: Set<string>,
  apiByClientId: Map<string, Message>,
  apiByRecordId?: Map<string, Message>,
): boolean {
  if (apiIds.has(m.id)) return true
  if (m.clientMessageId && apiByClientId.has(m.clientMessageId)) return true
  // [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md] 双字段范式：
  // 本地已认领 user（UI id=uuid, recordId=mc_ 指纹）
  // 与 API 权威版（id=后端 record_id，与 recordId 同值）收敛——按 recordId 命中
  // API id 集即同一条，刷新/补漏后不产生重复气泡
  if (m.recordId && apiIds.has(m.recordId)) return true
  if (m.recordId && apiByRecordId?.has(m.recordId)) return true
  return false
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
  const apiByRecordId = new Map<string, Message>()
  for (const m of apiSorted) {
    if (m.clientMessageId) apiByClientId.set(m.clientMessageId, m)
    if (m.recordId) apiByRecordId.set(m.recordId, m)
  }

  // 本地消息：被 API 覆盖 → 让位 API 版（丢弃本地乐观版）；其余全部保留（增量语义）。
  const keptLocal = existing.filter((m) => !isCoveredByApi(m, apiIds, apiByClientId, apiByRecordId))

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
      // addMessage 是乐观/流式/通知消息的入口（pending 乐观 user、ensureStreamingPlaceholder
      // 流式占位、handleSystemNotification 系统通知、send_failed 错误气泡），其
      // sequence 非后端权威值（无权威 seq 时挂空，本地不再拼号）。
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

  /** 认领 user 消息（[来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]）。
   *  裁决规则（纯逻辑见 src/streaming/claim.ts）：
   *  - 主数组候选命中 → upgrade：权威 record_id/seq 记入独立 recordId 字段，
   *    UI id 不变（React key 稳定），status='completed'
   *  - 候选缺失 → insert：以 cmid 为 id 补插权威 user 版（后端已持久化，不能丢）
   *  - 已认领/不匹配 → skip（幂等）
   *  乐观 user 消息自发送瞬间就在主数组（单一消息数组）——
   *  认领即原地升级，无 pending 区。 */
  claimUserMessage: (pipelineId, cmid, userRecord) => {
    const state = get()
    // 主数组候选：优先同 cmid（认领过的 recordId 幂等判据在裁决内）
    const mainMsgs = state.messagesByPipeline[pipelineId] || []
    const candidate = mainMsgs.find((m) => m.clientMessageId === cmid)
      || mainMsgs.find((m) => m.recordId === userRecord.id)

    const act = decideClaim(
      candidate
        ? { id: candidate.id, clientMessageId: candidate.clientMessageId, status: candidate.status, recordId: candidate.recordId }
        : undefined,
      userRecord,
      cmid,
    )

    if (act.kind === 'skip') return 'skipped'

    set((stateInner) => {
      const msgs = stateInner.messagesByPipeline[pipelineId] || []
      if (act.kind === 'upgrade') {
        const idx = msgs.findIndex((m) => m.id === act.messageId)
        const next = [...msgs]
        if (idx >= 0) {
          next[idx] = {
            ...next[idx],
            recordId: act.recordId,
            status: 'completed',
            ...(act.sequence != null ? { sequence: act.sequence } : {}),
          }
        } else {
          // 候选在主数组不存在（断线期间确认到达/刷新窗口）→ 补插权威版（保留 UI id）
          next.push({
            id: act.messageId,
            sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
            role: 'user',
            content: typeof userRecord.content === 'string' ? userRecord.content : '',
            timestamp: new Date().toISOString(),
            status: 'completed',
            clientMessageId: cmid,
            recordId: act.recordId,
            sequence: act.sequence,
          } as Message)
        }
        return {
          messagesByPipeline: { ...stateInner.messagesByPipeline, [pipelineId]: capMessagesForMemory(next) },
        }
      }
      // insert：以 cmid 为 UI id 补插（权威 user 已持久化，绝不能因本地缺候选而丢）
      if (msgs.some((m) => m.id === act.messageId || m.recordId === act.recordId)) {
        return stateInner
      }
      return {
        messagesByPipeline: {
          ...stateInner.messagesByPipeline,
          [pipelineId]: capMessagesForMemory([
            ...msgs,
            {
              id: act.messageId,
              sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
              role: 'user',
              content: typeof userRecord.content === 'string' ? userRecord.content : '',
              timestamp: new Date().toISOString(),
              status: 'completed',
              clientMessageId: cmid,
              recordId: act.recordId,
              sequence: act.sequence,
            } as Message,
          ]),
        },
      }
    })
    return act.kind === 'upgrade' ? 'upgraded' : 'inserted'
  },

  /** 无 cmid 注入消息（触发器/任务/HTTP）补插 user 气泡（ADR-2026-08-26）：
   *  按 id 幂等；按 sequence 落位（触发器消息出现在 assistant 回复之前）。 */
  ensureInjectedUserMessage: (pipelineId, userRecord) => {
    const recordId = userRecord.id
    if (!recordId) return
    set((stateInner) => {
      const msgs = stateInner.messagesByPipeline[pipelineId] || []
      // 幂等：recordId 或同内容同 seq 已存在 → 跳过（重复事件不双插）
      if (msgs.some((m) => m.recordId === recordId || m.id === recordId)) {
        return stateInner
      }
      const content = typeof userRecord.content === 'string' ? userRecord.content : ''
      const seq = userRecord.sequence
      const msg = {
        id: recordId,
        recordId,
        sessionId: stateInner.pipelineSessionMap[pipelineId] || '',
        role: 'user' as const,
        content,
        timestamp: new Date().toISOString(),
        status: 'completed' as const,
        ...(seq != null ? { sequence: seq } : {}),
        ...(userRecord.metadata ? { metadata: userRecord.metadata } : {}),
      } as Message
      // 按 sequence 落位：有 seq 则插到首个 seq 大于它的消息之前；
      // 无 seq 追加尾部（与后端消息序一致：user 先于其 assistant 回复）。
      let next: Message[]
      if (seq != null) {
        const idx = msgs.findIndex((m) => m.sequence != null && m.sequence > seq)
        next = idx >= 0 ? [...msgs.slice(0, idx), msg, ...msgs.slice(idx)] : [...msgs, msg]
      } else {
        next = [...msgs, msg]
      }
      return {
        messagesByPipeline: {
          ...stateInner.messagesByPipeline,
          [pipelineId]: capMessagesForMemory(next),
        },
      }
    })
  },

  /** 确认主数组乐观 user（旧内核无 user_message 回传的兼容路径）。 */
  confirmUserMessage: (pipelineId, cmid) => {
    let hit = false
    set((stateInner) => {
      const msgs = stateInner.messagesByPipeline[pipelineId] || []
      const idx = msgs.findIndex((m) => m.clientMessageId === cmid)
      if (idx < 0 || msgs[idx].status === 'completed') return stateInner
      hit = true
      const next = [...msgs]
      next[idx] = { ...next[idx], status: 'completed' }
      return {
        messagesByPipeline: { ...stateInner.messagesByPipeline, [pipelineId]: next },
      }
    })
    return hit
  },

  /** 更新指定管道中的消息（部分更新）。精确 ID 匹配：失配即
   * 记 error 并跳过——不做 role+sequence/指纹等模糊匹配（非唯一键，迟到事件
   * 会写错旧消息）。找不到目标说明事件与本地状态脱节，正确处置是等对账补正，
   * 而不是猜一条相近的写。 */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []

      const messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        logger.error(
          '[updateMessage] 目标消息不存在，跳过更新（精确匹配，不创建不模糊匹配）: pipelineId=%s messageId=%s',
          pipelineId?.slice(0, 12),
          messageId?.slice(0, 12),
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

  /** 获取指定管道中最后一条 user 消息 ID（重新生成缺省目标）。
   *  tool 结果消息不参与：截断点只能是 user 消息边界（tool_calls/tool 配对完整性）。 */
  findLastUserMessageId: (pipelineId: string) => {
    const msgs = get().messagesByPipeline[pipelineId] || []
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') return msgs[i].id
    }
    return null
  },

  /** 乐观截断：保留到目标 user 消息（含）为止，其后消息整体移除。
   *  重新生成/回退/编辑重发的本地即时反馈（服务端 regenerate 落库后由对账收敛）。
   *  找不到目标 user 消息 → 幂等跳过。 */
  truncateMessagesAfter: (pipelineId: string, userMessageId: string) => {
    set((state) => {
      const msgs = state.messagesByPipeline[pipelineId] || []
      const idx = msgs.findIndex((m) => m.id === userMessageId)
      if (idx < 0) return state
      const kept = msgs.slice(0, idx + 1)
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: kept,
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
    // 同步 context key：有任意管道运行中 → pipeline.running=true（ADR §3.4）
    useContextKeys.getState().setPipelineRunning(true)
  },

  /** 停止流式传输，同时将 assistant 消息状态标记为 interrupted（与 DB 一致：
   *  内核停止后半截消息落库 status="interrupted"，前端不显示"已完成"终态） */
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
          const stoppedMsg = updatedMessages[messageIndex]
          // 只收尾「流式 assistant」：sending/failed 的 user 乐观消息不归本方法管
          // （发送失败标 failed 后 stopStreaming 不得把它覆盖成 completed——
          // 单一消息数组：失败语义由调用方显式设置）。
          if (stoppedMsg.role === 'assistant') {
            // 收尾 part 状态：仅设置 message.status='interrupted' 会让 isStreamingMessage
            // 仍因残留的 'streaming'/'calling' part 返回 true（数据不一致，Stop 后再发新
            // 消息会卡在"思考中"）。与 handleStreamError 对齐：
            //   text/thinking 'streaming' -> 'done'
            //   tool_call 'calling'/'streaming' -> 'error'（abort 后未返回的工具调用视为失败）
            const finalizedParts = (stoppedMsg.parts || []).map((p) => {
              const partState = (p as { state?: string }).state
              if ((p.type === 'text' || p.type === 'thinking') && partState === 'streaming') {
                return { ...p, state: 'done' as const } as MessagePart
              }
              if (p.type === 'tool_call' && (partState === 'calling' || partState === 'streaming')) {
                return { ...p, state: 'error' as const } as MessagePart
              }
              return p
            })
            updatedMessages[messageIndex] = {
              ...stoppedMsg,
              parts: finalizedParts,
              status: 'interrupted',
              _lastUpdated: Date.now(),
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
      }

      return {
        streamingState: newStreamingState,
      }
    })
    // 同步 context key：无管道运行中时回 idle（ADR §3.4）
    const stillRunning = Object.keys(get().streamingState).length > 0
    useContextKeys.getState().setPipelineRunning(stillRunning)
  },

  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => {
    return get().streamingState[pipelineId]?.isStreaming ?? false
  },

  /** 冷启动：从 API 写入最新消息并设置双游标 合并策略 — API 权威替换本地缓存；未被 API 覆盖的「飞行中」本页消息（乐观 user / 流式占位 / 刚完成回复）在新鲜度窗口内保留。 */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      // 刷新语义：API 权威替换本地缓存。例外——快照发起早于本页消息活动时
      // （后台对账 fetch 与发送/流式收尾竞态：快照查询在消息落库前执行、响应
      // 在气泡出现后到达），快照天然不含这些消息，直接替换会把刚出现的气泡
      // 抹掉且无后续拉取补回（reconciled 已置 true、增量补漏仅重连触发）。
      // 乐观 user 与流式占位/刚完成回复都在主数组（单一消息数组协议），
      // 由本处新鲜度窗口统一保护：
      //   - 乐观 user：带 cmid 未被覆盖 + timestamp 在窗口内
      //   - assistant：_lastUpdated 在窗口内（流式占位与刚完成回复，流式路径必打戳）
      // 被 isCoveredByApi 命中（id/cmid/recordId）的让位 API 权威版——不并存、
      // 不重复；空白占位（无内容无 parts 且非流式）与超窗残留照丢，保持刷新
      // 去漂移语义。
      const apiIds = new Set(sorted.map((m) => m.id))
      const apiByClientId = new Map<string, Message>()
      const apiByRecordId = new Map<string, Message>()
      for (const m of sorted) {
        if (m.clientMessageId) apiByClientId.set(m.clientMessageId, m)
        if (m.recordId) apiByRecordId.set(m.recordId, m)
      }
      const now = Date.now()
      const isFresh = (ts: number | undefined) =>
        typeof ts === 'number' && now - ts <= INFLIGHT_FRESH_MS
      const inflight = filterBlankMessages(
        (existing || []).filter((m) => {
          if (isCoveredByApi(m, apiIds, apiByClientId, apiByRecordId)) return false
          if (m.role === 'user' && m.clientMessageId && isFresh(new Date(m.timestamp).getTime())) {
            return true
          }
          return m.role === 'assistant' && isFresh(m._lastUpdated)
        }),
      )

      // API 权威基底（空白过滤 + 内存封顶；游标只按它计算）
      const apiFinal = capMessagesForMemory(filterBlankMessages(sorted))
      const finalMessages = inflight.length > 0
        ? capMessagesForMemory(mergeSorted(apiFinal, [...inflight].sort(compareMessages)))
        : apiFinal

      // 游标只按 API 权威消息计算（appendMessages 已对齐同口径）：飞行消息的
      // 本地 seq/timestamp 不可信，不得污染 after_sequence 补漏窗口。
      const topCursor = apiFinal.length > 0 ? (apiFinal[0].sequence ?? 0) : 0
      const bottomCursor = calculateBottomCursor(apiFinal, state.bottomCursorsByPipeline[pipelineId])

      logger.info('[initFromAPI] done: pipelineId=%s finalMsgs=%d inflightKept=%d (API 权威替换，保留飞行中)',
        pipelineId?.slice(0, 12), finalMessages.length, inflight.length)

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
      // 合并新旧消息（保留已有 + 去重新增），不做 mergePreservingStreaming 二次合并——
      // 它内部的 mergeConsecutiveAssistantMessages 会把跨页边界的连续 assistant
      // 合并成一条，子管道（全是连续 tool 调用）50+50=100 条会被合并成 1-2 条，
      // 导致向上加载后消息几乎全丢。连续 assistant 的气泡合并由渲染层负责，
      // 数据层保持原始消息、sequence 连续。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
      // 过滤空白 assistant 消息（无 content 无 parts 无 toolCalls），避免空气泡
      const finalMerged = filterBlankMessages(merged)
      const topCursor = finalMerged[0]?.sequence ?? 0
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
      }
    })
  },

  /** 断线补漏：追加缺失消息到底部并更新 bottomCursor */
  appendMessages: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      if (messages.length === 0) return state
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      // 含 clientMessageId/recordId 双键对账：
      // user 乐观版（UUID id）与 API 权威版（record_id）同键时丢弃本地乐观版，
      // 避免切会话回来两条 user 并存。
      const merged = mergeIncrementalApiWithLocal(sorted, existing)
      // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
      const finalMerged = capMessagesForMemory(merged)
      // 游标只按 API 权威消息计算（对齐 initFromAPI 口径）：
      // 本地乐观/流式占位的 seq 不进游标，after_sequence 补漏不跳空。
      const apiFinal = capMessagesForMemory(filterBlankMessages(sorted))
      const bottomCursor = calculateBottomCursor(apiFinal, state.bottomCursorsByPipeline[pipelineId])
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

  /** 按 pipelineId 从 API 拉取消息（init/翻页/补漏三种游标模式） */
  fetchMessages: async (
    pipelineId: string,
    options?: { limit?: number; before_sequence?: number; after_sequence?: number; threadId?: string },
  ) => {
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
          const hasMoreOlder = apiResult.has_more
          get().prependMessages(pipelineId, mainMessages, hasMoreOlder)
        } else {
          // 首次冷启动：从 API 响应读取 has_more，避免首次返回 <50 条时被错误地标记为无更多历史消息
          const hasMoreOlder = apiResult.has_more
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
        // 重置「加载更早」的 loading 标记，避免残留 true 时用户滚动到顶部后「加载更多」完全失效。
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
    // 刷新后首屏：等 IndexedDB rehydrate 完成再决策——不等待时本地缓存不可见，
    // auto 会误判"未对账"走全量 init，缓存恢复路径被跳过。
    // init（显式强制全量）不需要缓存，跳过等待直接请求。
    if (mode !== 'init') await waitForMessageHydration()
    const state = get()
    const existingCount = (state.messagesByPipeline[pipelineId] || []).length

    // 流式保护：流式输出中且已有实质消息时跳过所有 API 调用。
    if (!skipStreamingCheck && state.isStreaming(pipelineId) && existingCount > 1) {
      return { ok: true }
    }

    // 模式决策：
    // - mode='init'：页面刷新 / 显式强制 → 全量 initFromAPI，丢弃本地，以 API 权威重建
    // - mode='auto'：切换会话 → 该管道尚未对账（刷新后首次进入 / 无缓存）：全量对账一次；
    //   已对账：不做任何 API 调用，直接用缓存
    // - mode='backfill'：WS 重连补漏 → 增量追加
    // 对账标记的信任条件：rehydrate 后 reconciledByPipeline 被重置为 {}，
    // 但 IndexedDB 缓存里的消息本体仍有效（persist 快照只含已确认消息）。刷新后首次
    // auto 进入：本地有缓存 → 页面立即用缓存渲染（秒开），同时后台静默全量对账
    // （修正断线空洞/残影，见 reconcileFromAPI）；仅缓存缺失/为空时才同步全量重建。
    const reconciled = state.reconciledByPipeline[pipelineId] ?? false
    const hasLocalMessages = existingCount > 0
    const isInit = mode === 'init' || (mode === 'auto' && !reconciled && !hasLocalMessages)
    const needReconcile = mode === 'auto' && !reconciled && hasLocalMessages
    const needBackfill = mode === 'backfill'

    try {
      if (isInit) {
        await state.fetchMessages(pipelineId, { threadId })
        set((s) => ({ reconciledByPipeline: { ...s.reconciledByPipeline, [pipelineId]: true } }))
      } else if (needReconcile) {
        // 刷新后缓存秒开 + 后台静默全量对账（不等结果，修正空洞/残影）
        void reconcileFromAPI(pipelineId, threadId)
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
      // 复活链已废除：rehydrate 一律丢弃 streaming/占位消息——
      // 刷新后的飞行中内容由两条成熟机制恢复：WS 重连 replay（last_sequence
      // watermark 回放事件流，前端按精确 ID 重建）+ 完成后 backfill（API 权威
      // 记录）。持久化快照只保留已确认消息，杜绝「刷新后本地残影复活」通道。
      const cleanedMessages: Record<string, Message[]> = {}
      if (p.messagesByPipeline) {
        for (const [pid, msgs] of Object.entries(p.messagesByPipeline)) {
          if (!msgs) continue
          cleanedMessages[pid] = msgs.filter((m) => m.status !== 'streaming')
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
      // 唤醒 waitForMessageHydration 的等待者（rehydrate 完成 = 缓存可见，
      // loadPipelineMessages 的 auto 决策不再误判）
      _markHydrated()
    },
  },
))
