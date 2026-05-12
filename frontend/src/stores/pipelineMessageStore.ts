/**
 * 统一管道消息状态管理 Store
 *
 * 将 sessionStore.messages（主管道）和 agentTabStore.tabMessages（子管道）统一为
 * 以 pipelineId 为一级索引的消息存储，消除跨 Store 直接操作的问题。
 */

import { create } from 'zustand'
import { messageApi } from '@/services/api/messages'
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

  /** 删除指定管道中的消息（乐观更新 + API 调用） */
  deleteMessage: (pipelineId: string, messageId: string, includeTarget?: boolean) => void

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

      // 精确匹配失败时，assistant 消息尝试模糊匹配
      if (messageIndex < 0 && partial.role === 'assistant' && partial.timestamp) {
        messageIndex = pipelineMessages.findIndex((m) =>
          m.role === 'assistant'
          && m.timestamp === partial.timestamp,
        )
      }

      if (messageIndex < 0) {
        return state
      }

      const updatedMessages = [...pipelineMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        ...partial,
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
   * BUG-FIX-fix_20260512_session_switch_msg_loss:
   * 问题根因: 用户在会话A发送消息后切换到会话B再切回A时，setActiveSession 调用
   *          fetchMessages → initFromAPI，后者直接替换 pipelineA 的全部消息。
   *          由于读后写竞争（WebSocket user_input 刚到达后端，GET /messages 尚未包含），
   *          API 返回的数据可能不包含刚发送的用户消息，导致乐观更新的用户消息被覆盖丢失。
   *          用户消息丢失后，连续的 assistant 消息被 mergeConsecutiveAssistantMessages 合并，
   *          AI 回复渲染到上一个 assistant 气泡中。刷新后正常因为后端已持久化完整数据。
   * 修复方案: 当管道已有消息时，采用合并策略而非替换策略：
   *          1. 保留 status=streaming 的本地消息（正在生成的 AI 回复）
   *          2. 保留不在 API 响应中的本地消息（乐观更新的用户消息等），
   *             但排除已由 API 确认的重复消息（通过 role+content+时间窗口匹配）
   *          3. API 返回的消息作为权威数据覆盖同 ID 的本地消息
   * 影响范围: 会话切换时的消息显示完整性
   * 修复日期: 2026-05-12
   */
  initFromAPI: (pipelineId: string, messages: Message[]) => {
    set((state) => {
      const sorted = [...messages].sort(compareMessages)
      const existing = state.messagesByPipeline[pipelineId]

      let finalMessages: Message[]

      if (existing && existing.length > 0) {
        const apiIds = new Set(sorted.map((m) => m.id))

        const preserved = existing.filter((localMsg) => {
          if (localMsg.status === 'streaming') return true

          if (!apiIds.has(localMsg.id)) {
            const hasApiMatch = sorted.some(
              (apiMsg) =>
                apiMsg.role === localMsg.role
                && apiMsg.content === localMsg.content
                && Math.abs(
                  new Date(apiMsg.timestamp).getTime()
                  - new Date(localMsg.timestamp).getTime(),
                ) < 10000,
            )
            return !hasApiMatch
          }

          return false
        })

        if (preserved.length > 0) {
          finalMessages = [...sorted, ...preserved]
          finalMessages.sort(compareMessages)
        } else {
          finalMessages = sorted
        }
      } else {
        finalMessages = sorted
      }

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
      try {
        const limit = options?.limit ?? 50
        // BUG-FIX-fix_20260512_fetch_threadid:
        // 问题根因: 多处调用 fetchMessages(pipelineId) 未传 threadId，
        //          导致子管道的 pipelineId 被当作 threadId 去查后端 /threads/{threadId}/messages，
        //          后端找不到对应线程，返回 404/500 错误，消息不显示。
        // 修复方案: 自动从 pipelineSessionMap 查找 pipelineId 对应的 sessionId 作为 threadId fallback，
        //          并通过 pipelines 元数据判断是否为子管道（level > 1），正确传 pipelineRunId。
        // 影响范围: 切换标签、WS 重连、删除消息后重载等所有 fetchMessages 调用场景
        // 修复日期: 2026-05-12
        const sessionFallback = get().pipelineSessionMap[pipelineId]
        const threadId = options?.threadId || sessionFallback || pipelineId
        const pipelineMeta = get().pipelines[pipelineId]
        const isSubPipeline = pipelineMeta && pipelineMeta.level > 1
        const apiResult = await retry(
          () => apiGetMessages(threadId, {
            limit,
            before_sequence: options?.before_sequence,
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
        // BUG-FIX-fix_20260512_msg_disappear:
        // 问题根因: 之前过滤掉所有带 parentId 的消息，导致子 Agent 回复等消息在刷新后消失。
        //          后端 message_view.py 的 MessageQueryBuilder 已通过 is_current 条件
        //          确保只返回当前版本的消息，不需要前端再额外过滤。
        // 修复方案: 移除 parentId 过滤，保留所有从 API 返回的消息。
        //          ChatContainer 的 mergeConsecutiveAssistantMessages 会处理连续 assistant 消息的合并显示。
        // 影响范围: 页面刷新后的消息加载
        // 修复日期: 2026-05-12
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
          console.error(
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

  /**
   * 删除指定管道中的消息（乐观更新 + API 调用）
   *
   * 从 sessionStore.deleteMessage 迁移而来，以 pipelineId 为索引。
   * 逻辑：
   * 1. 根据 messageId 定位目标消息
   * 2. 根据 includeTarget 决定删除范围（目标及之后 / 仅之后）
   * 3. 递归查找子消息一并删除
   * 4. 乐观更新前端状态，异步调用后端 API
   * 5. API 成功后重新加载消息确保一致性；失败则回滚
   */
  deleteMessage: (pipelineId: string, messageId: string, includeTarget: boolean = true) => {
    logger.debug('[pipelineMessageStore.deleteMessage] 开始删除:', {
      pipelineId,
      messageId,
      includeTarget,
    })

    // 保存回滚用快照
    const previousMessages = get().messagesByPipeline[pipelineId] || []

    set((state) => {
      const pipelineMessages = state.messagesByPipeline[pipelineId] || []
      const targetMessage = pipelineMessages.find((m) => m.id === messageId)

      if (!targetMessage) {
        logger.warn('[pipelineMessageStore.deleteMessage] 未找到要删除的消息')
        return state
      }

      const targetSequence = targetMessage.sequence || 0
      const targetParentId = targetMessage.parentId || null

      // 收集同 parentId 下满足条件的消息 ID
      const mainRecordIds = new Set<string>()
      pipelineMessages.forEach((m) => {
        const mParentId = m.parentId || null
        const mSequence = m.sequence || 0
        if (mParentId === targetParentId) {
          if (includeTarget) {
            if (mSequence >= targetSequence) {
              mainRecordIds.add(m.id)
            }
          } else {
            if (mSequence > targetSequence) {
              mainRecordIds.add(m.id)
            }
          }
        }
      })

      const allIdsToDelete = new Set<string>(mainRecordIds)
      if (includeTarget) {
        allIdsToDelete.add(targetMessage.id)
      }

      // 递归查找子消息
      let currentParentIds = Array.from(allIdsToDelete)
      while (currentParentIds.length > 0) {
        const childrenIds: string[] = []
        pipelineMessages.forEach((m) => {
          const mParentId = m.parentId || null
          if (mParentId && currentParentIds.includes(mParentId)) {
            if (!allIdsToDelete.has(m.id)) {
              childrenIds.push(m.id)
            }
          }
        })

        if (childrenIds.length === 0) {
          break
        }

        childrenIds.forEach((id) => allIdsToDelete.add(id))
        currentParentIds = childrenIds
      }

      const updatedMessages = pipelineMessages.filter((m) => !allIdsToDelete.has(m.id))

      logger.debug('[pipelineMessageStore.deleteMessage] 删除结果:', {
        原始数量: pipelineMessages.length,
        删除后数量: updatedMessages.length,
        删除的消息数: pipelineMessages.length - updatedMessages.length,
      })

      return {
        messagesByPipeline: {
          ...state.messagesByPipeline,
          [pipelineId]: updatedMessages,
        },
      }
    })

    // 异步调用后端 API
    logger.debug('[pipelineMessageStore.deleteMessage] 调用后端 API 删除')
    messageApi
      .deleteMessage(pipelineId, messageId, includeTarget)
      .then(async (result) => {
        logger.debug('[pipelineMessageStore.deleteMessage] 后端删除成功:', result)
        try {
          // 重新加载消息以确保一致性
          await get().fetchMessages(pipelineId)
        } catch (reloadError) {
          logger.warn('[pipelineMessageStore.deleteMessage] 重新加载消息失败，保持乐观更新状态:', reloadError)
        }
      })
      .catch((error: unknown) => {
        const errorAny = error as Record<string, any>
        const errorCode = errorAny?.code
        const errorStatus = errorAny?.response?.status ?? errorAny?.status
        if (errorCode === '404' || errorCode === 404 || errorStatus === 404) {
          logger.warn('[pipelineMessageStore.deleteMessage] 消息已被删除，保持前端状态')
          return
        }

        console.error('[pipelineMessageStore.deleteMessage] 删除消息失败，回滚前端状态:', error)

        // 回滚到之前的状态
        set((state) => ({
          messagesByPipeline: {
            ...state.messagesByPipeline,
            [pipelineId]: previousMessages,
          },
        }))

        throw error
      })
  },

}))
