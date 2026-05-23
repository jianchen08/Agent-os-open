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
import { useStreamingStore } from './streamingStore'
import type { Message } from '@/types/models'

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
   * BUG-FIX-fix_20260523_pipeline_mismatch:
   * 当 stream_start 中的 pipelineId 与用户消息写入的管道不一致时，
   * 将旧管道中最近的用户消息复制到新管道，确保用户消息和助手响应在同一管道中显示。
   */
  migrateRecentUserMessages: (fromPipelineId: string, toPipelineId: string) => void

  /** 直接从 API 加载指定管道的历史消息 */
  fetchMessages: (
    pipelineId: string,
    options?: { limit?: number; before_sequence?: number; after_sequence?: number; threadId?: string },
  ) => Promise<void>
}

/**
 * 消息排序比较函数：先按 sequence 升序，再按 timestamp 升序
 */
function compareMessages(a: Message, b: Message): number {
  const seqA = a.sequence ?? Number.MAX_SAFE_INTEGER
  const seqB = b.sequence ?? Number.MAX_SAFE_INTEGER
  if (seqA !== seqB) {
    return seqA - seqB
  }
  return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
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

      // 精确 ID 匹配去重
      let existingIndex = pipelineMessages.findIndex((m) => m.id === realMessageId)

      // BUG-FIX-fix_20260515_long_context_message_loss:
      // 精确匹配失败时，尝试基于 sequence 或指纹的模糊匹配
      // 避免同一消息因 WS/API ID 格式不同被当作两条消息
      if (existingIndex < 0 && message.sequence != null) {
        existingIndex = pipelineMessages.findIndex((m) =>
          m.sequence === message.sequence,
        )
      }
      if (existingIndex < 0 && message.role === 'assistant') {
        // 优先用 sequence 匹配（sequence 在会话内唯一）
        if (message.sequence != null) {
          existingIndex = pipelineMessages.findIndex((m) =>
            m.role === 'assistant' && m.sequence === message.sequence,
          )
        }
        // sequence 匹配失败时，回退到 role + timestamp（保持兼容）
        if (existingIndex < 0 && message.timestamp) {
          existingIndex = pipelineMessages.findIndex((m) =>
            m.role === 'assistant'
            && m.timestamp
            && m.timestamp === message.timestamp,
          )
        }
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
        updatedMessages = [
          ...pipelineMessages,
          { ...message, id: realMessageId },
        ]
        // 非激活管道收到新消息时增加未读计数
        if (state.activePipelineId !== pipelineId) {
          unreadChanged = true
        }
      }

      updatedMessages.sort(compareMessages)

      const newPipelines = { ...state.pipelines }
      if (unreadChanged && newPipelines[pipelineId]) {
        newPipelines[pipelineId] = {
          ...newPipelines[pipelineId],
          unreadCount: newPipelines[pipelineId].unreadCount + 1,
        }
      }

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
        pipelines: newPipelines,
      }
    })
  },

  /**
   * 更新指定管道中的消息（部分更新），支持模糊匹配
   */
  updateMessage: (pipelineId: string, messageId: string, partial: Partial<Message>) => {
    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []

      let messageIndex = pipelineMessages.findIndex((m) => m.id === messageId)

      // BUG-FIX-fix_20260515_long_context_message_loss:
      // 精确匹配失败时，assistant 消息尝试模糊匹配
      // 优先使用 sequence 匹配（sequence 在会话内唯一），回退到 role + timestamp
      if (messageIndex < 0 && partial.role === 'assistant') {
        // 优先用 sequence 精确匹配
        if (partial.sequence != null) {
          messageIndex = pipelineMessages.findIndex((m) =>
            m.role === 'assistant' && m.sequence === partial.sequence,
          )
        }
        // sequence 匹配失败时，回退到 role + timestamp（保持兼容）
        if (messageIndex < 0 && partial.timestamp) {
          messageIndex = pipelineMessages.findIndex((m) =>
            m.role === 'assistant'
            && m.timestamp === partial.timestamp,
          )
        }
      }

      if (messageIndex < 0) {
        // FIX: 找不到消息时打印 warn 日志以便排查
        logger.warn(
          `[updateMessage] message not found: pipelineId=%s messageId=%s totalMsgs=%d`,
          pipelineId?.slice(0, 12), messageId?.slice(0, 12), pipelineMessages.length,
        )
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        ...partial,
        // BUG-FIX-fix_20260522_msg_disappear: 记录消息最后更新时间，防止 initFromAPI 覆盖近期 WS 更新
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
    useStreamingStore.getState().setStreamingForTab(pipelineId, false)

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
   * 删除模糊的时间窗口匹配逻辑（WS 和 API 竞争由 streaming 保留逻辑覆盖）。
   */
  initFromAPI: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      logger.info('[initFromAPI] pipelineId=%s apiMsgs=%d existingMsgs=%d',
        pipelineId?.slice(0, 12), sorted.length, existing?.length || 0)

      let finalMessages: Message[]
      // BUG-FIX-fix_20260522_msg_disappear: 跟踪被保留的本地消息数量
      let preservedCount = 0

      if (existing && existing.length > 0) {
        // BUG-FIX-fix_20260515_long_context_message_loss:
        // 原逻辑只保留 streaming 消息，导致 WS 已接收但 API 尚未同步的消息被丢弃。
        // 修复方案: 使用指纹去重，逐条检查旧消息是否在新消息列表中有匹配，
        //          只丢弃已被 API 数据覆盖的旧消息，保留 API 尚未返回的有效消息。
        const apiFingerprints = new Map<string, Message[]>()
        for (const m of sorted) {
          const fp = makeMessageFingerprint(m)
          const arr = apiFingerprints.get(fp) || []
          arr.push(m)
          apiFingerprints.set(fp, arr)
        }

        // 逐条过滤旧消息：仅在指纹匹配且新消息列表中确实存在对应条目时才移除旧消息
        const matchedApiIds = new Set<string>()
        // BUG-FIX-fix_20260522_msg_disappear: 记录因近期 WS 更新被保护的本地消息对应的 API 消息 ID
        const excludedApiIds = new Set<string>()
        const preserved = existing.filter((localMsg) => {
          // streaming 消息始终保留（API 可能还没有这条消息）
          if (localMsg.status === 'streaming') {
            const apiIds = new Set(sorted.map((m) => m.id))
            return !apiIds.has(localMsg.id)
          }

          const fp = makeMessageFingerprint(localMsg)
          const candidates = apiFingerprints.get(fp)
          if (!candidates) return true // 没有匹配的API消息 -> 保留这条WS消息
          // 检查是否有尚未被匹配的 API 消息
          const unmatched = candidates.find((c) => !matchedApiIds.has(c.id))
          if (unmatched) {
            matchedApiIds.add(unmatched.id)

            // BUG-FIX-fix_20260522_msg_disappear: 保护近期通过 WS 更新的消息不被 API 空内容覆盖
            const localUpdated = (localMsg as Message & { _lastUpdated?: number })._lastUpdated
            if (localUpdated && Date.now() - localUpdated < 5000) {
              const localLen = (localMsg.content || '').length
              const apiLen = (unmatched.content || '').length
              if (apiLen < localLen * 0.5) {
                excludedApiIds.add(unmatched.id)
                return true // 保留本地版本，排除对应 API 消息
              }
            }

            return false // 这条旧消息被新 API 消息替代
          }
          return true // API 消息已全部匹配完毕，保留旧消息
        })

        // BUG-FIX-fix_20260522_msg_disappear: 排除被本地版本保护的 API 消息，避免重复
        const filteredSorted = excludedApiIds.size > 0
          ? sorted.filter((m) => !excludedApiIds.has(m.id))
          : sorted

        preservedCount = preserved.length

        if (preserved.length > 0) {
          finalMessages = [...filteredSorted, ...preserved]
          finalMessages.sort(compareMessages)
        } else {
          finalMessages = filteredSorted
        }
      } else {
        finalMessages = sorted
      }

      // BUG-FIX-fix_20260522_msg_disappear: 诊断日志，记录合并结果
      logger.info('[initFromAPI] done: pipelineId=%s finalMsgs=%d preserved=%d',
        pipelineId?.slice(0, 12), finalMessages.length, preservedCount)

      const topCursor = finalMessages.length > 0 ? (finalMessages[0].sequence ?? 0) : 0
      const bottomCursor = finalMessages.length > 0
        ? finalMessages.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0)
        : 0
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
      const merged = [...newMsgs, ...existing]
      merged.sort(compareMessages)
      const topCursor = sorted[0].sequence ?? 0
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
      const merged = [...existing, ...newMsgs]
      merged.sort(compareMessages)
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
   * BUG-FIX-fix_20260523_pipeline_mismatch:
   * 问题根因: 用户消息写入 sid 管道（fallback），但后端创建新 pipeline 后
   *          stream_start 激活了新管道，导致用户消息和助手消息不在同一管道。
   * 修复方案: 将旧管道中最近的用户消息复制到新管道中。
   *          使用 addMessage 的去重机制确保不会重复。
   * 影响范围: 新会话首次消息发送、刷新页面后发送消息
   * 修复日期: 2026-05-23
   */
  migrateRecentUserMessages: (fromPipelineId: string, toPipelineId: string) => {
    // 同一管道无需迁移
    if (fromPipelineId === toPipelineId) return

    const fromMessages = get().messagesByPipeline[fromPipelineId] || []
    if (fromMessages.length === 0) return

    // 找到旧管道中最近的用户消息（可能有连续多条用户消息，都需要迁移）
    // 从后往前找到所有连续的 user 消息
    const userMessagesToMigrate: Message[] = []
    for (let i = fromMessages.length - 1; i >= 0; i--) {
      if (fromMessages[i].role === 'user') {
        userMessagesToMigrate.unshift(fromMessages[i])
      } else {
        break
      }
    }

    if (userMessagesToMigrate.length === 0) return

    logger.info(
      '[migrateRecentUserMessages] 迁移 %d 条用户消息: %s -> %s',
      userMessagesToMigrate.length,
      fromPipelineId?.slice(0, 12),
      toPipelineId?.slice(0, 12),
    )

    // 使用 addMessage 逐条写入新管道（利用其去重机制）
    for (const msg of userMessagesToMigrate) {
      get().addMessage(toPipelineId, { ...msg })
    }
  },

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

    // 并发去重：如果该 pipelineId 已有正在进行的请求，直接复用该 Promise
    const existingFetch = _fetchingPipelines.get(pipelineId)
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
        // 请求完成后从去重映射中移除
        _fetchingPipelines.delete(pipelineId)
      }
    })()

    // 记录正在进行的请求
    _fetchingPipelines.set(pipelineId, fetchPromise)

    return fetchPromise
  },

}))
