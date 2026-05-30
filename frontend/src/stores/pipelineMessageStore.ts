/**
 * 统一管道消息状态管理 Store
 *
 * 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为
 * 以 pipelineId 为一级索引的消息存储，消除跨 Store 直接操作的问题。
 */

import { create } from 'zustand'
import { getMessages as apiGetMessages } from '@/services/api/session'
import { loggers } from '@/utils/logger'
import { retry, isRetryableError } from '@/utils/retry'
import type { Message } from '@/types/models'
import type { MessagePart, ToolCallPart } from '@/types/messageParts'

const logger = loggers.sessionStore

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
  /** 获取指定管道的消息列表 */
  getMessages: (pipelineId: string) => Message[]

  /** 开始流式传输 */
  startStreaming: (pipelineId: string, messageId: string) => void
  /** 停止流式传输 */
  stopStreaming: (pipelineId: string) => void
  /** 查询指定管道是否正在流式传输 */
  isStreaming: (pipelineId: string) => boolean

  /** 冷启动：从 API 写入最新消息并设置双游标 */
  initFromAPI: (pipelineId: string, messages: Message[]) => void
  /** 向上翻页：将更早消息插入头部并更新 topCursor */
  prependMessages: (pipelineId: string, messages: Message[]) => void
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

  /**
   * 将旧管道中最近的用户消息迁移到新管道
   *
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
 *
 * BUG-FIX-fix_20260529_sequence_race:
 * 新增 id 第三级排序：当 sequence 和 timestamp 都相同时，按 id 排序确保结果稳定。
 * 避免 sequence 相同时消息顺序不确定导致 UI 闪烁或错乱。
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
  // BUG-FIX-fix_20260529_sequence_race: 第三级排序用 id，确保 sequence + timestamp 相同时排序稳定
  const idA = a.id || ''
  const idB = b.id || ''
  return idA < idB ? -1 : idA > idB ? 1 : 0
}

/** 二分查找插入位置，保持数组按 compareMessages 排序 */
function bisectInsertIndex(arr: Message[], msg: Message): number {
  let lo = 0
  let hi = arr.length
  while (lo < hi) {
    const mid = (lo + hi) >>> 1
    if (compareMessages(arr[mid], msg) < 0) {
      lo = mid + 1
    } else {
      hi = mid
    }
  }
  return lo
}

/** 将 msg 插入已排序数组 arr 的正确位置，返回新数组 */
function sortedInsert(arr: Message[], msg: Message): Message[] {
  const idx = bisectInsertIndex(arr, msg)
  return [...arr.slice(0, idx), msg, ...arr.slice(idx)]
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
 * 合并 API 消息与本地已有消息
 *
 * 简化策略：
 * 1. 本地无消息 → 直接用 API 数据
 * 2. 本地有 streaming 消息 → 只保留 streaming 消息（API 未返回同 ID 的），其余用 API 数据
 * 3. 本地无 streaming 消息 → 直接用 API 数据（不保留本地消息，避免重复）
 *
 * @param sorted - 已排序的 API 消息列表
 * @param existing - 本地已有消息列表（可能为 undefined）
 * @returns { finalMessages: 合并后的消息列表, preservedCount: 保留的本地消息数 }
 */
function mergeApiWithExisting(
  sorted: Message[],
  existing: Message[] | undefined,
): { finalMessages: Message[]; preservedCount: number } {
  // 本地无消息，直接用 API 数据
  if (!existing || existing.length === 0) {
    return { finalMessages: sorted, preservedCount: 0 }
  }

  // 只保留本地 streaming 消息（API 未返回同 ID 的）
  const apiIds = new Set(sorted.map((m) => m.id))
  const streamingOnly = existing.filter((m) => 
    m.status === 'streaming' && !apiIds.has(m.id)
  )

  // 无 streaming 消息需要保留 → 直接用 API 数据
  if (streamingOnly.length === 0) {
    return { finalMessages: sorted, preservedCount: 0 }
  }

  // 有 streaming 消息需要保留 → 合并
  const finalMessages = mergeSorted(sorted, streamingOnly)
  return { finalMessages, preservedCount: streamingOnly.length }
}

/**
 * 计算 bottom 游标（只增不减，防止流式消息 sequence 临时值导致回退）
 *
 * BUG-FIX-fix_20260529_bottom_cursor_regression:
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
 */
export const usePipelineMessageStore = create<PipelineMessageState>()((set, get) => ({
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

      let existingIndex = pipelineMessages.findIndex((m) => m.id === realMessageId)

      if (existingIndex < 0 && message.sequence != null) {
        existingIndex = pipelineMessages.findIndex((m) =>
          m.sequence === message.sequence && m.role === message.role,
        )
      }

      if (existingIndex >= 0) {
        const oldMsg = pipelineMessages[existingIndex]
        console.warn(
          `[MSG-LIFE] ★ addMessage 更新: id=%s role=%s oldStatus=%s newStatus=%s oldContentLen=%d newContentLen=%d oldPartsLen=%d newPartsLen=%d`,
          realMessageId?.slice(0, 12), message.role,
          oldMsg.status, (message as any).status,
          (oldMsg.content || '').length, ((message as any).content || '').length,
          (oldMsg.parts || []).length, ((message as any).parts || []).length,
        )
      } else {
        console.warn(
          `[MSG-LIFE] ★ addMessage 新增: id=%s role=%s status=%s contentLen=%d seq=%s totalMsgs=%d`,
          realMessageId?.slice(0, 12), message.role, (message as any).status,
          ((message as any).content || '').length, message.sequence ?? '-',
          pipelineMessages.length,
        )
      }

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
        updatedMessages = sortedInsert(pipelineMessages, { ...message, id: realMessageId })
        // 非激活管道收到新消息时增加未读计数
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

      if (messageIndex < 0) {
        if (partial.role !== 'user') {
          console.warn(
            `[MSG-LIFE] ★ updateMessage 未找到: id=%s pipeline=%s`,
            messageId?.slice(0, 12), pipelineId?.slice(0, 12),
          )
        }
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
   * 开始流式传输，记录正在流式传输的消息 ID
   */
  startStreaming: (pipelineId: string, messageId: string) => {
    set((state) => ({
      streamingState: {
        ...state.streamingState,
        [pipelineId]: {
          isStreaming: true,
          messageId,
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
   */
  initFromAPI: (pipelineId: string, messages: Message[]) => {
    console.warn(
      `[DATA-SOURCE] ★ initFromAPI 数据: pipeline=%s msgs=%d`,
      pipelineId?.slice(0, 12), messages.length,
    )
    for (const m of messages.slice(0, 5)) {
      console.table({
        id: (m.id as string).slice(0, 12),
        role: m.role,
        contentLen: (m.content || '').length,
        contentPreview: (m.content || '').slice(0, 60),
        partsLen: (m.parts || []).length,
        partsTypes: (m.parts || []).map((p: any) => p.type).join(','),
        seq: m.sequence,
      })
    }
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      const { finalMessages, preservedCount } = mergeApiWithExisting(sorted, existing)

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
          [pipelineId]: messages.length >= 50,
        },
      }
    })
  },

  /**
   * 向上翻页：将更早消息插入头部并更新 topCursor
   */
  prependMessages: (pipelineId: string, messages: Message[]) => {
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
      const merged = mergeSorted(newMsgs, existing)
      const topCursor = merged[0].sequence ?? 0
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
          [pipelineId]: messages.length >= 50,
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
        // FIX: 自动从 pipelineSessionMap 查找 sessionId 作为 threadId fallback，子管道正确传 pipelineRunId
        const sessionFallback = get().pipelineSessionMap[pipelineId]
        const threadId = options?.threadId || sessionFallback || pipelineId
        const pipelineMeta = get().pipelines[pipelineId]
        const isSubPipeline = pipelineMeta && pipelineMeta.level > 1
        const apiResult = await retry(
          () => apiGetMessages(threadId, {
            limit,
            before_sequence: options?.before_sequence,
            after_sequence: options?.after_sequence,
            pipelineRunId: isSubPipeline ? pipelineId : undefined,
          }),
          {
            maxAttempts: 3,
            delayMs: 1000,
            shouldRetry: (error) => {
              const status = error?.response?.status ?? error?.status
              if (status === 429) return true
              if (status === 404) return false
              return isRetryableError(error)
            },
          },
        )

        const rawMessages: Message[] = apiResult.messages || []
        // FIX: 后端 MessageQueryBuilder 已确保只返回当前版本消息，前端不再额外过滤 parentId
        const mainMessages = rawMessages

        if (options?.after_sequence !== undefined) {
          get().appendMessages(pipelineId, mainMessages)
        } else if (options?.before_sequence !== undefined) {
          get().prependMessages(pipelineId, mainMessages)
        } else {
          get().initFromAPI(pipelineId, mainMessages)
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

}))
