/**
 * 统一管道消息状态管理 Store
 *
 * 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为
 * 以 pipelineId 为一级索引的消息存储，消除跨 Store 直接操作的问题。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
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
 * 并发去重：跟踪正在进行的 fetch 请求，避免同一 pipelineId 重复请求
 */
const _fetchingPipelines = new Map<string, Promise<void>>()

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
  // 指纹集合：覆盖 WS UUID 与 API hex id 不一致但实际为同一条消息的场景（如 AI 消息）
  const apiFingerprints = new Set(sorted.map((m) => makeMessageFingerprint(m)))

  // 本地独有的消息（API 没有的）：正在流式或未持久化的乐观消息
  const localOnly = existing.filter((m) => {
    if (apiIds.has(m.id)) return false
    if (m.clientMessageId && apiByClientId.has(m.clientMessageId)) return false
    // 指纹匹配：同 role+sequence 视为同一条消息（覆盖 WS UUID vs API hex 场景）
    if (apiFingerprints.has(makeMessageFingerprint(m))) return false
    return true
  })

  if (localOnly.length === 0) {
    return { finalMessages: sorted, preservedCount: 0 }
  }

  // API 权威消息在前（按后端顺序），本地独有消息追加在后（按到达顺序）
  return { finalMessages: [...sorted, ...localOnly], preservedCount: localOnly.length }
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

      // #region debug-point G:add-message
      try { fetch("http://127.0.0.1:7777/event",{method:"POST",body:JSON.stringify({sessionId:"chat-scroll-render",runId:"pre",hypothesisId:"G",location:"store:addMessage",msg:"[ADD] "+message.role,data:{pid:pipelineId?.slice(0,12),mid:realMessageId?.slice(0,12),role:message.role,seq:message.sequence,status:message.status,total:pipelineMessages.length+1},ts:Date.now()})}).catch(()=>{}); } catch {}
      // #endregion

      let existingIndex = pipelineMessages.findIndex((m) => m.id === realMessageId)

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
          [pipelineId]: updatedMessages,
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

      const oldMsg = pipelineMessages[messageIndex]
      console.warn(
        `[MSG-LIFE] ★ updateMessage: id=%s oldStatus=%s → newStatus=%s oldContentLen=%d → newContentLen=%d`,
        messageId?.slice(0, 12), oldMsg.status, (partial as any).status || oldMsg.status,
        (oldMsg.content || '').length, ((partial as any).content ?? (oldMsg.content || '')).length,
      )

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
      finalMessages = mergeConsecutiveAssistantMessages(finalMessages)
      // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
      finalMessages = filterBlankMessages(finalMessages)

      const topCursor = finalMessages.length > 0 ? (finalMessages[0].sequence ?? 0) : 0
      // #region debug-point B:init-result
      try { fetch("http://127.0.0.1:7777/event",{method:"POST",body:JSON.stringify({sessionId:"chat-scroll-render",runId:"pre",hypothesisId:"B",location:"pipelineMessageStore.ts:initFromAPI",msg:"[DEBUG] initFromAPI result",data:{pipelineId:pipelineId?.slice(0,12),apiMsgs:sorted.length,existing:existing?.length||0,afterMerge:finalMessages.length,msgs:finalMessages.map(m=>({id:m.id?.slice(0,8),role:m.role[0],seq:m.sequence,cLen:(m.content||'').length,pLen:(m.parts||[]).length}))},ts:Date.now()})}).catch(()=>{}); } catch {}
      // #endregion
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
      merged = mergeConsecutiveAssistantMessages(merged)
      // 过滤空白 assistant 消息（无 content 无 parts），避免空气泡
      merged = filterBlankMessages(merged)
      const topCursor = merged[0]?.sequence ?? 0
      // #region debug-point A:prepend-result
      try { fetch("http://127.0.0.1:7777/event",{method:"POST",body:JSON.stringify({sessionId:"chat-scroll-render",runId:"pre",hypothesisId:"A",location:"pipelineMessageStore.ts:prepend",msg:"[DEBUG] prepend result",data:{pipelineId:pipelineId?.slice(0,12),newMsgs:newMsgs.length,beforeMerge:existing.length+newMsgs.length,afterMerge:merged.length,msgs:merged.map(m=>({id:m.id?.slice(0,8),role:m.role[0],seq:m.sequence,cLen:(m.content||'').length,pLen:(m.parts||[]).length}))},ts:Date.now()})}).catch(()=>{}); } catch {}
      // #endregion
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
          [pipelineId]: merged,
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
    console.warn('[STORE] fetchMessages: pipeline=%s before=%s after=%s', pipelineId?.slice(0,12), options?.before_sequence, options?.after_sequence)
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
        // DEBUG: 确认 has_more 是否正确传递
        console.warn('[fetchMessages] API result: msgs=%d has_more=%s', rawMessages.length, apiResult.has_more)
        // FIX: 后端 MessageQueryBuilder 已确保只返回当前版本消息，前端不再额外过滤 parentId
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
      return {
        ...current,
        ...p,
        // 运行时状态强制重置（不信任持久化值）
        streamingState: {},
        isLoadingOlderByPipeline: {},
      }
    },
  },
))
