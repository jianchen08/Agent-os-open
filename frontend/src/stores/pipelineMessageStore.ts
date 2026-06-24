/**
 * 统一管道消息状态管理 Store
 *
 * 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为
 * 以 pipelineId 为一级索引的消息存储，消除跨 Store 直接操作的问题。
 */

import { create } from 'zustand'
import { persist, createJSONStorage } from 'zustand/middleware'
import { getMessages as apiGetMessages, mergeConsecutiveAssistantMessages } from '@/services/api/session'
import { loggers } from '@/utils/logger'
// retry removed per audit: 内部 API 不应内置重试，429/5xx 重试统一由 axios interceptor 管理
import type { Message } from '@/types/models'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'

const logger = loggers.sessionStore

/**
 * 每个管道持久化的最大消息条数
 *
 * BUG-FIX-fix_20260622_workspace_state_loss:
 * 为防止 localStorage 配额溢出，每个 pipeline 仅持久化最近 N 条消息（与 agentTabStore 一致）。
 * 更早的消息会在恢复后从 API 重新加载（向上翻页）。
 */
const PERSIST_MAX_MESSAGES_PER_PIPELINE = 50

/**
 * 乐观消息的"宽限期"
 *
 * BUG-FIX-fix_20260623_optimistic_user_msg_vanish:
 * mergeApiWithExisting 合并 API 与本地消息时，未被 API 命中的本地 user 消息
 * 若在此时间窗口内（且带 clientMessageId）则保留为乐观消息，
 * 等待后端持久化后下一次 fetch 由 clientMessageId 对账替换。
 * 超过此窗口仍未被 API 命中视为脏数据丢弃。
 *
 * 取值 30s：覆盖正常后端持久化延迟（通常 <1s），同时短于 persist
 * 残留消息的常见时间间隔，避免误保留旧脏数据。
 */
const OPTIMISTIC_MSG_GRACE_MS = 30_000

/**
 * 判断本地独有消息（API 未返回的）是否落在「刚生成、后端可能尚未持久化」的宽限期内。
 *
 * BUG-FIX-fix_20260624_ai_msg_vanish:
 * 与乐观 user 消息（fix_20260623_optimistic_user_msg_vanish）同源问题——
 * AI 回复 stream_end/finalizeMessage 后 status 由 'streaming' 变 'completed'，
 * isStreamingMessage() 随即返回 false。此时若 initFromAPI 被并发触发
 * （WS 重连 / Tab 切换 / 会话切换）且后端尚未持久化该 AI 消息，
 * API 返回列表不含它 → 走 return false 被丢弃 → 最新几条 AI 回复消失，
 * 刷新后才重新出现，表现与 user 消息完全一致。
 *
 * 每类消息用单一字段判定「乐观窗口起点」：
 *   - user 消息用 timestamp：乐观消息由 addMessage 创建（不写 _lastUpdated），
 *     timestamp 即发送时刻。
 *   - assistant 消息用 _lastUpdated：stream_end 的 finalize/updateMessage 必写此字段，
 *     即「刚完成」时刻。persist 残留的旧 AI 消息 _lastUpdated 为上次会话值或缺失，
 *     均不满足窗口条件，被正确丢弃，不重新引入重复渲染。
 */
function isWithinOptimisticGrace(m: Message): boolean {
  if (m.role === 'user' && m.clientMessageId) {
    return Date.now() - new Date(m.timestamp).getTime() < OPTIMISTIC_MSG_GRACE_MS
  }
  if (m.role === 'assistant' && typeof m._lastUpdated === 'number') {
    return Date.now() - m._lastUpdated < OPTIMISTIC_MSG_GRACE_MS
  }
  return false
}

/**
 * 持久化数据的有效期策略说明（非强制 TTL）
 *
 * BUG-FIX-fix_20260622_workspace_state_loss:
 * 此处不实现强制 TTL 清理，原因：
 * 1. 数据恢复价值高于过期风险——重登后 initFromAPI 会用 API 权威数据覆盖持久化数据，
 *    过期数据自然被替换，不会造成脏读。
 * 2. merge 时强制重置 streamingState/loading 等运行时状态，避免恢复陈旧的流式标记。
 * 3. 每个 pipeline 限制 50 条消息（PERSIST_MAX_MESSAGES_PER_PIPELINE），
 *    数据量可控，无需激进清理。
 * 若未来需要强制 TTL，可在 onRehydrateStorage 中读取 savedAt 并按需清除。
 */

/**
 * 裁剪每个 pipeline 的消息列表，仅保留最近 N 条用于持久化
 *
 * BUG-FIX-fix_20260622_workspace_state_loss:
 * 防止 localStorage 配额溢出。更早的消息会在恢复后从 API 重新加载（向上翻页）。
 * 保留最新消息确保用户回到会话时立即看到最近的对话上下文。
 */
function trimMessagesForPersistence(
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
 * 单个管道在「内存」中保留的最大消息条数
 *
 * 与 PERSIST_MAX_MESSAGES_PER_PIPELINE（仅持久化裁剪）不同：内存里的
 * messagesByPipeline 此前没有上限，长会话 + 向上翻页只增不减。叠加
 * MessageList 全量渲染（已弃用虚拟列表）与 persist 每次 set 全量序列化，
 * 最终撑爆浏览器内存 → V8 抛 Out of Memory、tab 崩溃、WS 随之断开
 * （后端表现为"前端连不上"）。
 *
 * 取 300：覆盖日常长会话与多次翻页，仅在极端长会话触发；超出时丢弃
 * sequence 最小的（最老）消息，它们仍可从 API 重新加载（向上翻页恢复）。
 */
const MAX_MESSAGES_PER_PIPELINE_IN_MEMORY = 300

/**
 * 限制单管道内存消息数，防止无限增长导致浏览器 OOM。
 *
 * 仅在超量时裁剪：按 sequence 排序后保留最新的 N 条。未超限时只做一次
 * length 比较（early return），零额外开销。正常翻页（每批 ~50 条）不会
 * 触及 300 上限；极端长会话退化为"最近窗口"，配合 API 翻页兜底。
 */
function capMessagesForMemory(msgs: Message[]): Message[] {
  if (msgs.length <= MAX_MESSAGES_PER_PIPELINE_IN_MEMORY) return msgs
  return [...msgs].sort(compareMessages).slice(-MAX_MESSAGES_PER_PIPELINE_IN_MEMORY)
}

/**
 * 并发去重：跟踪正在进行的 fetch 请求，避免同一 pipelineId 重复请求
 */
const _fetchingPipelines = new Map<string, Promise<void>>()

/**
 * 容错 + 节流持久化 storage
 *
 * BUG-FIX-fix_20260623_persist_quota_blocks_business:
 * 问题根因: zustand persist 在 set/api.setState 路径同步调用 storage.setItem，
 *   localStorage 配额满时抛 QuotaExceededError，异常冒泡到 addMessage/initFromAPI/
 *   fetchMessages，阻断消息加载（用户看到"加载失败"且新消息不显示）。
 * 修复方案: 自定义 storage 包装 localStorage.setItem，捕获并吞掉写入异常，
 *   持久化失败仅记录一次 warn（防刷屏），内存 state 与业务流程不受影响。
 *
 * BUG-FIX-fix_20260624_persist_throttle_oom:
 * 问题根因: 流式输出期间 appendToPart 高频触发 store set，persist 随之对整个
 *   messagesByPipeline 做 JSON.stringify + 同步写 localStorage（阻塞主线程）。
 *   单条超长回复流式时每帧一次的大对象序列化是内存与主线程峰值的主要来源，
 *   叠加全量渲染，直接把浏览器推到 Out of Memory。
 * 修复方案: setItem 改为 trailing 节流 + maxWait 上限。窗口内持续到达的写入
 *   反复推迟落盘并合并为最后一次（流式每帧的中间态本就不可信）；但持续高频
 *   写入超过 maxWait 后强制落盘一次，保证长流式不会无限积压在缓冲。
 *   内存 state 不受影响（已即时更新），仅延迟落盘。节流窗口内刷新会丢失
 *   该窗口的持久化，但刷新后由 initFromAPI 从 API 重新加载权威数据。
 * 影响范围: 流式输出期间的主线程占用与内存峰值；持久化时效性（延迟至多 5s）
 * 修复日期: 2026-06-24
 */
const PERSIST_THROTTLE_MS = 1000
// 持续高频写入时强制落盘的上限：避免长流式（>1s）期间缓冲永远推迟落盘。
// 取 5s：远大于单帧间隔，能合并绝大部分流式抖动；又足够短，崩溃时最多丢 5s。
const PERSIST_MAX_WAIT_MS = 5000
const _persistQuotaWarned = { current: false }
// 节流缓冲：只缓存最新一次 (name, value)。本 store 仅一个持久化 key，
// 流式期间多次 set 合并为最后一次，trailing 定时器统一落盘。
const _persistBuffer: { name: string; value: string } = { name: '', value: '' }
let _persistTimer: ReturnType<typeof setTimeout> | null = null
// 首次进入当前节流窗口的时刻：用于判断是否已达 maxWait 需强制落盘
let _persistWindowStartedAt = 0

/** 实际落盘（容错）：取出缓冲的最后一次写入执行，失败仅记一次 warn */
function _writePersisted(): void {
  _persistTimer = null
  _persistWindowStartedAt = 0
  const { name, value } = _persistBuffer
  _persistBuffer.name = ''
  _persistBuffer.value = ''
  if (!name) return
  try {
    window.localStorage.setItem(name, value)
  } catch (err) {
    // 配额满或禁用：仅记录一次 warn，避免每次 set 都刷屏
    if (!_persistQuotaWarned.current) {
      _persistQuotaWarned.current = true
      logger.warn(
        '[pipelineMessageStore] 持久化失败（localStorage 配额耗尽或不可用），'
        + '本次会话内消息仅保存在内存，刷新后将从 API 重新加载: err=%s',
        err,
      )
    }
  }
}

/**
 * 调度一次节流落盘。
 * - 窗口内已有挂起写入：推迟到当前窗口结束（合并）；
 * - 已超 maxWait：立即落盘，不再推迟（防长流式积压）；
 * - 无挂起写入：开启新窗口。
 */
function _schedulePersist(): void {
  if (_persistTimer !== null) {
    // 持续高频：若已达 maxWait 上限则强制落盘，否则保持现有 trailing 定时器
    if (Date.now() - _persistWindowStartedAt >= PERSIST_MAX_WAIT_MS) {
      clearTimeout(_persistTimer)
      _writePersisted()
    }
    return
  }
  _persistWindowStartedAt = Date.now()
  _persistTimer = setTimeout(_writePersisted, PERSIST_THROTTLE_MS)
}

const tolerantJsonStorage = createJSONStorage(() => ({
  getItem: (name) => {
    try {
      return window.localStorage.getItem(name)
    } catch {
      return null
    }
  },
  setItem: (name, value) => {
    // 节流：缓存最新写入，trailing 定时器到点统一落盘，避免高频 set 同步写盘
    _persistBuffer.name = name
    _persistBuffer.value = value
    _schedulePersist()
  },
  removeItem: (name) => {
    // 清理时取消挂起的节流写入，避免 remove 后又被 trailing 写回
    if (_persistTimer !== null) {
      clearTimeout(_persistTimer)
      _persistTimer = null
    }
    _persistWindowStartedAt = 0
    _persistBuffer.name = ''
    _persistBuffer.value = ''
    try {
      window.localStorage.removeItem(name)
    } catch {
      /* 忽略清理失败 */
    }
  },
}))

/**
 * 管道元数据
 */
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

/**
 * 单个管道的流式状态
 */
export interface StreamingStatus {
  /** 是否正在流式传输 */
  isStreaming: boolean
  /** 正在流式传输的消息 ID */
  messageId: string | null
}

/**
 * Store 状态接口
 */
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

  /** 直接从 API 加载指定管道的历史消息 */
  fetchMessages: (
    pipelineId: string,
    options?: { limit?: number; before_sequence?: number; after_sequence?: number; threadId?: string },
  ) => Promise<void>

  // ==================== Parts 统一修改方法 ====================

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
 * 消息排序比较函数：先按 sequence 升序，再按 timestamp 升序
/**
 * BUG-FIX-fix_20260617_blank_message_filter:
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

/**
 * 判断消息是否处于流式状态（不可参与合并）
 *
 * BUG-FIX-fix_20260622_streaming_msg_merged:
 * 问题根因: initFromAPI / prependMessages 在合并连续 assistant 消息时，
 *   会把正在流式输出的 assistant 消息（status='streaming' 或 parts 含
 *   state='streaming' 的 part）卷入相邻历史消息的合并组，导致流式 part
 *   被重编、被丢弃或与历史消息混合，表现为切换页面后文本气泡重复。
 * 修复方案: 流式消息作为「分隔符」打断合并组，自身不参与合并。
 * 影响范围: 切换页面/向上翻页时流式消息的渲染稳定性
 * 修复日期: 2026-06-22
 */
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

/**
 * 合并连续 assistant 消息，但保护流式消息不被卷入合并
 *
 * 流式消息作为分隔符：它本身不合并，且会打断其前后的连续 assistant 组。
 * 这样历史消息（API 返回的已完成消息）仍能跨边界合并，而正在流式的消息
 * 保持独立完整。
 *
 * BUG-FIX-fix_20260622_streaming_msg_merged
 */
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
    if (isStreamingMessage(msg)) {
      flush()
      result.push(msg)
    } else {
      segment.push(msg)
    }
  }
  flush()
  return result
}

/**
 * 排序键优先级：sequence → timestamp → id（确保 sequence/timestamp 相同时排序稳定）。
 */
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

/**
 * 生成消息指纹，用于跨 ID 格式（WS UUID vs API hex）去重
 *
 * BUG-FIX-fix_20260515_long_context_message_loss:
 * 问题根因: 原去重逻辑使用 role::timestamp 作为指纹，在长会话中
 *          多条消息可能共享相同的 role+timestamp（例如工具调用后紧跟文本回复），
 *          导致 initFromAPI 合并时错误地将有效消息当作重复消息丢弃。
 * 修复方案: 使用 sequence 作为主要去重键（sequence 在会话内唯一递增），
 *          sequence 不可用时回退到 role::timestamp::contentPrefix 提高区分度。
 */
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
 * 合并 API 权威消息与本地已有消息
 *
 * 策略：
 * 1. 本地无消息 → 直接用 API 数据（首次加载，按后端 sequence 排序）
 * 2. 本地有消息 → 以 API 顺序为基准，本地独有的消息（正在流式/未持久化）追加到末尾
 *    这样历史消息按后端保存顺序，实时消息按到达顺序，两者不打架。
 *
 * BUG-FIX-fix_20260617_ai_msg_dup_render:
 * 问题根因: AI 消息无 clientMessageId，WS UUID 和 API hex 是不同 id，
 *           原去重逻辑识别为不同消息，导致切换 Tab 时上一条 AI 消息重复渲染。
 * 修复方案: 增加 makeMessageFingerprint 指纹去重（基于 role::seq），同指纹视为同一条消息。
 * 影响范围: 切换会话/Tab、流式消息与 API 数据合并
 * 修复日期: 2026-06-17
 */
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
  // 2. 刚发送的乐观 user 消息（30s 窗口内，带 clientMessageId） — 保留，
  //    因为后端可能尚未持久化，API 尚未返回。
  // 3. 其余本地消息（completed 历史、persist 残留的脏数据） — 丢弃，
  //    以 API 权威数据为准。
  //
  // BUG-FIX-fix_20260623_local_completed_msg_orphan:
  //   非 streaming 的本地消息 API 未匹配上则丢弃（return false），
  //   防止 localStorage 残留的旧消息每次刷新被恢复保留导致重复渲染。
  //
  // BUG-FIX-fix_20260623_optimistic_user_msg_vanish:
  //   问题根因: 上述"全部丢弃"策略会误杀刚发送的乐观 user 消息——
  //     用户发消息 → addMessage(乐观 user) → fetchMessages/initFromAPI 被触发
  //     （WS 重连 / Tab 切换 / 会话切换）→ 后端尚未持久化 user 消息 →
  //     API 返回数据不含该消息 → 乐观消息被丢弃 → 用户消息消失，
  //     表现为"发送的消息不显示，刷新后才出现"。
  //   修复方案: 带 clientMessageId 的 user 消息在 OPTIMISTIC_MSG_GRACE_MS（30s）
  //     时间窗口内保留，覆盖后端持久化的正常延迟。
  //     persist 残留的旧消息不满足时间条件（timestamp 远超 30s），仍被丢弃，
  //     不重新引入重复渲染。
  //
  // BUG-FIX-fix_20260624_ai_msg_vanish:
  //   问题根因: 同源问题影响 AI 消息。AI 回复 stream_end/finalize 后 status 变为
  //     'completed'，isStreamingMessage() 返回 false，不再受「streaming 占位符保留」保护。
  //     此时若 initFromAPI 并发触发且后端尚未持久化该 AI 消息，API 不含它 → 走
  //     return false 被丢弃 → 最新几条 AI 回复消失，刷新后才出现（与 user 消息同症状）。
  //   修复方案: 宽限期守卫扩展到刚 finalize 完成的 assistant 消息，由 isWithinOptimisticGrace
  //     统一判定。AI 消息无 clientMessageId，改用 _lastUpdated（finalize 写入）界定「刚完成」。
  //     后续 initFromAPI 通过 role::seq 指纹去重用 API 权威版本替换，不引入重复。
  const localOnly = existing.filter((m) => {
    if (apiIds.has(m.id)) return false
    if (m.clientMessageId && apiByClientId.has(m.clientMessageId)) return false
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
  //
  // BUG-FIX-fix_20260617_ai_msg_dup_render:
  //   streaming 占位符与 API 权威消息 id 不同（WS UUID vs API hex），靠 role::seq
  //   指纹识别为同一条，避免切换 Tab 时 AI 气泡重复渲染。
  // BUG-FIX-fix_20260624_ai_msg_vanish:
  //   宽限期保留的 completed assistant 消息同理——后端已持久化时 API 会返回同 seq
  //   权威版本，此时去重只留一份，避免「保留乐观版 + API 版」并存成两条。
  //   后端未持久化时 API 不含该指纹，localOnly 版本正常保留，等待下次 fetch 对账。
  const localOnlyFingerprints = new Set(localOnly.map((m) => makeMessageFingerprint(m)))
  const dedupedSorted = localOnlyFingerprints.size
    ? sorted.filter((m) => !localOnlyFingerprints.has(makeMessageFingerprint(m)))
    : sorted

  // BUG-FIX-fix_20260623_refresh_order:
  // 问题根因: 原代码用 [...sorted, ...localOnly] 直接末尾拼接，未按 sequence
  //   合并排序。刷新后 persist 恢复的 localOnly 消息（旧 sequence）会被错误地
  //   排到所有 API 返回的新消息之后，导致页面刷新后消息顺序错乱、与后端数据不一致。
  // 修复方案: 用 mergeSorted 按 sequence 升序归并 API 权威消息与本地独有消息，
  //   与 appendMessages/prependMessages 保持一致。initFromAPI 后续的
  //   mergePreservingStreaming/filterBlankMessages 不改变顺序，最终渲染顺序正确。
  //   注意：mergeSorted 要求两个输入各自升序，localOnly 来自 existing（可能无序，
  //   如 persist 恢复或并发写入），需先排序。
  // 影响范围: 页面刷新、会话切换后消息顺序
  // 修复日期: 2026-06-23
  const sortedLocalOnly = [...localOnly].sort(compareMessages)
  return { finalMessages: mergeSorted(dedupedSorted, sortedLocalOnly), preservedCount: localOnly.length }
}

/**
 * 计算 bottom 游标（只增不减，防止流式消息 sequence 临时值导致回退）
 *
 * 取 max(API 返回的最大 seq, 现有 bottomCursor)，只增不减。
 */
function calculateBottomCursor(finalMessages: Message[], existingCursor: number | undefined): number {
  const apiBottomCursor = finalMessages.length > 0
    ? finalMessages.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
    : 0
  return Math.max(apiBottomCursor, existingCursor ?? 0)
}


/**
 * 统一管道消息 Store
 *
 * BUG-FIX-fix_20260622_workspace_state_loss:
 * 加 persist 中间件持久化核心状态，避免整页刷新/重登后丢失工作区消息。
 */
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

  /**
   * 注册管道，建立 pipelineId 与元数据的映射
   */
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

  /**
   * 激活管道，同时重置该管道的未读计数
   */
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

  /**
   * 添加消息到指定管道，自动去重和排序
   */
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

      // 同步更新 bottomCursor
      const newBottomCursors = { ...state.bottomCursorsByPipeline }
      const newSeq = message.sequence
      if (newSeq != null) {
        const currentBottom = newBottomCursors[pipelineId] ?? 0
        if (newSeq > currentBottom) {
          newBottomCursors[pipelineId] = newSeq
        }
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
          [pipelineId]: capMessagesForMemory(updatedMessages),
        },
        pipelines: newPipelines,
        bottomCursorsByPipeline: newBottomCursors,
      }
    })
  },

  /**
   * 更新指定管道中的消息（部分更新），支持模糊匹配
   *
   * 注意：找不到消息时不会自动创建，仅输出 warn 日志。
   * 消息创建应统一走 addMessage 或 ensureStreamingPlaceholder。
   */
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

      // BUG-FIX-fix_20260617_upsert_dup:
      // 问题根因: 原代码在找不到消息时无条件 upsert 创建，stream_end/new_message 的 messageId
      //          与本地占位符不一致（pipeline_id 漂移、后端重发等）时会把别的消息内容当新消息追加。
      // 修复方案: 创建前先用指纹（role::seq）兜底查找，找到则更新而非创建。
      // 影响范围: 流式消息与 API 数据合并时的重复消息创建
      // 修复日期: 2026-06-17
      if (messageIndex < 0) {
        if (partial.sequence != null) {
          const fingerprint = (partial.role || 'assistant') + '::seq::' + partial.sequence
          messageIndex = pipelineMessages.findIndex((m) => makeMessageFingerprint(m) === fingerprint)
        }
      }

      // BUG-FIX-fix_20260617_remove_upsert_fallback:
      // 问题根因: 原代码在所有匹配都失败时 upsert 创建新消息，导致 stream_end/new_message
      //          的 messageId 与本地占位符不一致时把别的消息内容当新消息追加，造成重复渲染。
      // 修复方案: 彻底删除 upsert 创建，找不到目标消息时仅记 error 日志，不修改 store。
      //          真正的消息补漏由 ensureStreamingPlaceholder 等显式创建路径负责。
      // 影响范围: 流式消息更新路径，避免重复消息
      // 修复日期: 2026-06-17
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

  /**
   * 获取指定管道的消息列表
   */
  getMessages: (pipelineId: string) => {
    return get().messagesByPipeline[pipelineId] || []
  },

  /**
   * 移除指定管道中的消息
   *
   * BUG-FIX-fix_20260530_empty_bubble:
   * 用于移除空的 assistant 占位消息（无内容无 parts），
   * 避免在系统通知场景下出现空气泡。
   */
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

  /**
   * 开始流式传输，记录正在流式传输的消息 ID
   */
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

  /**
   * 停止流式传输，同时将消息状态标记为 completed
   */
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

  /**
   * 查询指定管道是否正在流式传输
   */
  isStreaming: (pipelineId: string) => {
    return get().streamingState[pipelineId]?.isStreaming ?? false
  },

  /**
   * 冷启动：从 API 写入最新消息并设置双游标
   *
   * FIX: 合并策略 — streaming 消息仅在 API 未返回同 ID 时保留，其余以 API 数据为准。
   *
   * BUG-FIX-fix_20260617_ai_msg_dup_render:
   *   见 mergeApiWithExisting 的指纹去重方案；initFromAPI 调用 mergeApiWithExisting
   *   合并 API 数据与本地 WS 流式消息，避免 AI 消息因 id/clientMessageId 不一致被重复渲染。
   */
  initFromAPI: (pipelineId: string, messages: Message[], hasMoreOlder?: boolean) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      let { finalMessages, preservedCount } = mergeApiWithExisting(sorted, existing)

      // BUG-FIX-fix_20260617_init_boundary_merge:
      // 合并 API 数据与本地流式消息后，边界处可能有连续 assistant 消息需要合并
      // BUG-FIX-fix_20260622_streaming_msg_merged:
      // 流式消息（status='streaming' 或 parts 含 streaming part）不参与合并，
      // 它们作为分隔符打断连续 assistant 组，避免 part 被重编或丢弃。
      finalMessages = mergePreservingStreaming(finalMessages)
      // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
      finalMessages = filterBlankMessages(finalMessages)
      // 内存封顶：超量时丢弃最老消息，防止长会话撑爆内存（OOM）
      finalMessages = capMessagesForMemory(finalMessages)

      const topCursor = finalMessages.length > 0 ? (finalMessages[0].sequence ?? 0) : 0
      const bottomCursor = calculateBottomCursor(finalMessages, state.bottomCursorsByPipeline[pipelineId])

      logger.info('[initFromAPI] done: pipelineId=%s finalMsgs=%d preserved=%d',
        pipelineId?.slice(0, 12), finalMessages.length, preservedCount)

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

  /**
   * 向上翻页：将更早消息插入头部并更新 topCursor
   */
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
      const existingIds = new Set(existing.map((m) => m.message_id || m.id))
      const newMsgs = sorted.filter((m) => !existingIds.has(m.message_id || m.id))
      let merged = mergeSorted(newMsgs, existing)
      // BUG-FIX-fix_20260617_prepend_boundary_merge:
      // 问题根因: mergeConsecutiveAssistantMessages 只在 API 层对单次返回的消息合并，
      //   向上翻页加载时，新加载的消息和已有消息边界处可能有连续 assistant 消息
      //   无法跨边界合并，导致同一个 AI 回复被拆成多个气泡（"多重渲染"）。
      // 修复方案: prepend 后对完整列表重新执行 assistant 合并，消除分页边界。
      // 影响范围: 向上翻页时的消息渲染
      // 修复日期: 2026-06-17
      //
      // BUG-FIX-fix_20260622_streaming_msg_merged:
      // 流式消息不参与合并，保护其 part sequence 不被重编。
      merged = mergePreservingStreaming(merged)
      // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
      merged = filterBlankMessages(merged)
      // 内存封顶：翻页累计超量时丢弃最老消息，防止撑爆内存（OOM）
      merged = capMessagesForMemory(merged)
      const topCursor = merged[0]?.sequence ?? 0
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: merged,
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

  /**
   * 断线补漏：追加缺失消息到底部并更新 bottomCursor
   */
  appendMessages: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      if (messages.length === 0) return state
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId] || []
      const existingIds = new Set(existing.map((m) => m.message_id || m.id))
      const newMsgs = sorted.filter((m) => !existingIds.has(m.message_id || m.id))
      const merged = mergeSorted(existing, newMsgs)
      const bottomCursor = merged.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          // 内存封顶：超量时丢弃最老消息，防止撑爆内存（OOM）
          [pipelineId]: capMessagesForMemory(merged),
        },
        bottomCursorsByPipeline: {
          ...state.bottomCursorsByPipeline,
          [pipelineId]: bottomCursor,
        },
      }
    })
  },

  /**
   * 获取指定管道的顶部游标
   */
  getTopCursor: (pipelineId: string) => {
    return get().topCursorsByPipeline[pipelineId] ?? 0
  },

  /**
   * 获取指定管道的底部游标
   */
  getBottomCursor: (pipelineId: string) => {
    return get().bottomCursorsByPipeline[pipelineId] ?? 0
  },

  /**
   * 判断指定管道是否已初始化
   */
  isInitialized: (pipelineId: string) => {
    return pipelineId in get().messagesByPipeline
  },

  /**
   * 判断指定管道是否还有更早的消息
   */
  hasMoreOlder: (pipelineId: string) => {
    return get().hasMoreOlderByPipeline[pipelineId] ?? false
  },

  /**
   * 将旧管道中最近的用户消息迁移到新管道
   *
  /**
   * 直接从 API 加载指定管道的历史消息
   *
   * 替代 sessionStore.fetchMessages + ChatContainer 同步 effect 的双跳模式，
   * 直接调 API 并写入 pipelineMessageStore，消除双 Store 同步的时序竞争。
   */
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
        // FIX: 自动从 pipelineSessionMap 查找 sessionId 作为 threadId fallback
        // FEATURE-pipeline_unify: 统一传 pipelineRunId（主/子管道都用 pipelineId），
        //                         后端统一走 pipelineRunId 路径，不再区分主/子。
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
        // 后端 MessageQueryBuilder 已确保只返回当前版本消息，前端不再额外过滤 parentId
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
          logger.warn(
            '[pipelineMessageStore.fetchMessages] 加载失败（已重试）: pipelineId=%s err=%s',
            pipelineId, err,
          )
        }
      } finally {
        _fetchingPipelines.delete(dedupeKey)
        // BUG-FIX-fix_20260529_scroll_load_more:
        // 问题根因: finally 块只删除了去重键，没有重置 isLoadingOlderByPipeline，
        //          当 API 请求失败时 loading 状态永远卡在 true，后续请求被跳过，
        //          导致用户滚动到顶部后"加载更多"完全失效。
        // 修复方案: 在 finally 中确保 isLoadingOlderByPipeline 被重置为 false。
        // 影响范围: 向上翻页加载更多消息功能
        // 修复日期: 2026-05-29
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

  // ==================== Parts 统一修改方法 ====================

  /**
   * 追加一个新 Part 到指定消息
   */
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

  /**
   * 更新指定消息的某个 Part（按 partIndex 精确定位）
   */
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

  /**
   * 向指定 Part 追加文本内容（用于流式增量）
   */
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

  /**
   * 结束消息流式状态：所有 Part.state = 'done', 消息 status = 'completed'
   */
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

  /**
   * 获取指定消息中最后一个指定类型的 Part 的 index
   */
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

  /**
   * 获取指定消息中 state='streaming' 的最后一个 Part 的 index
   */
  findStreamingPartIndex: (pipelineId: string, messageId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    for (let i = msg.parts.length - 1; i >= 0; i--) {
      const p = msg.parts[i]
      if ((p.type === 'text' || p.type === 'thinking') && p.state === 'streaming') return i
    }
    return -1
  },

  /**
   * 获取指定消息中指定 callId 的 tool_call Part 的 index
   */
  findToolCallPartIndex: (pipelineId: string, messageId: string, callId: string) => {
    const state = get()
    const pipelineMessages = state.messagesByPipeline[pipelineId]
    if (!pipelineMessages) return -1
    const msg = pipelineMessages.find((m) => m.id === messageId)
    if (!msg || !msg.parts) return -1
    return msg.parts.findIndex((p) => p.type === 'tool_call' && (p as ToolCallPart).callId === callId)
  },
}),
  // BUG-FIX-fix_20260622_workspace_state_loss:
  // 问题根因: 原本 messagesByPipeline/activePipelineId 等核心状态纯内存，
  //          整页刷新（含认证失效重定向）后全部丢失，用户感受到"工作没了"。
  // 修复方案: 加 persist 中间件持久化核心状态，重登/刷新后自动恢复。
  //          - 只持久化消息、管道元数据、活跃 pipeline（运行时状态如 streaming/loading 不持久化）
  //          - 每个 pipeline 限制 50 条消息防止 localStorage 溢出
  //          - 24 小时 TTL，过期数据由 API 重新加载覆盖
  {
    name: 'pipeline-messages',
    version: 1,
    // 容错 storage：localStorage 配额满时吞掉 setItem 异常，不阻断业务
    storage: tolerantJsonStorage,
    // 仅持久化核心数据，排除运行时状态
    partialize: (state) => ({
      messagesByPipeline: trimMessagesForPersistence(state.messagesByPipeline),
      pipelines: state.pipelines,
      pipelineSessionMap: state.pipelineSessionMap,
      activePipelineId: state.activePipelineId,
      topCursorsByPipeline: state.topCursorsByPipeline,
      bottomCursorsByPipeline: state.bottomCursorsByPipeline,
      hasMoreOlderByPipeline: state.hasMoreOlderByPipeline,
    }),
    // 恢复时合并默认状态（运行时状态用默认值）
    merge: (persisted, current) => {
      const p = (persisted as Partial<PipelineMessageState>) || {}
      // 恢复的消息中所有 status='streaming' 的占位符强制标记为 completed。
      // BUG-FIX-fix_20260623_orphan_streaming_persist:
      // 问题根因: 上次会话 AI 回复进行中（占位符 status='streaming'）时刷新/关闭，
      //   persist 存下了 streaming 占位符。重新打开后它被原样恢复，但管道级
      //   streamingState 已重置为空——这条占位符成了 orphan streaming。
      //   initFromAPI 时它走 isStreamingMessage() return true 被保留，与 API 返回的
      //   completed 权威消息并存 → AI 气泡重复渲染（user 消息已修复但 AI 仍重复）。
      // 修复方案: 与 streamingState 同理，恢复时不信任消息内的运行时状态。
      //   把所有恢复消息的 status='streaming' 改为 'completed'，parts 内
      //   state='streaming'/'calling' 的 part 改为 'done'。这样 orphan 占位符要么被
      //   initFromAPI 用 id/clientMessageId 匹配上（API 已有真实版本），
      //   要么走 return false 丢弃，不再保留为孤儿。
      const cleanedMessages: Record<string, Message[]> = {}
      if (p.messagesByPipeline) {
        for (const [pid, msgs] of Object.entries(p.messagesByPipeline)) {
          if (!msgs) continue
          cleanedMessages[pid] = msgs.map((m) => {
            if (m.status === 'streaming') {
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
      }
    },
  },
))
