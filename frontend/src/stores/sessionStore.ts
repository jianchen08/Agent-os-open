import { create } from 'zustand'
import { messageApi } from '@/services/api/messages'
import { getMessages } from '@/services/api/session'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { WebSocketStatus } from '@/services/websocket/WebSocketService'
import { loggers } from '@/utils/logger'
import { useMessageVersionStore } from './messageVersionStore'
import type { MessageVersion } from './messageVersionStore'
import type { Message, RetryScope, Session } from '@/types/models'

const logger = loggers.sessionStore

const sessionApi = {
  getMessages,
}

interface MessagePaginationState {
  hasMore: boolean
  /** 最旧已加载消息的 sequence，用于游标分页 */
  oldestSequence: number | null
  limit: number
  isLoadingMore: boolean
}

interface SessionState {
  sessions: Session[]
  activeSessionId: string | null
  /**
   * 保留 messages 状态，供 retryMessage、版本管理等场景使用。
   * 主消息数据已迁移到 pipelineMessageStore.messagesByPipeline。
   */
  messages: Record<string, Message[]>
  isLoading: boolean
  deletingSessionIds: Set<string>
  error: string | null
  wsStatus: string
  forceReconnect: boolean
  messagePagination: Record<string, MessagePaginationState>
  _wsUnsubscribers: { cleanup: () => void } | null

  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => void
  fetchMessages: (
    sessionId: string,
    options?: { limit?: number; before_sequence?: number; append?: boolean },
  ) => Promise<void>
  fetchSubMessages: (sessionId: string, parentId: string) => Promise<void>
  loadMoreMessages: (sessionId: string) => Promise<void>
  getMessagePagination: (sessionId: string) => MessagePaginationState
  connectWebSocket: (sessionId: string, token: string) => void
  disconnectWebSocket: () => void
  clearError: () => void
  retryMessage: (
    sessionId: string,
    messageId: string,
    scope?: RetryScope,
    targetToolId?: string,
  ) => Promise<void>
  createMessageVersion: (sessionId: string, messageId: string) => number
  restoreMessageVersion: (sessionId: string, messageId: string, version: number) => void
  getMessageVersions: (
    sessionId: string,
    messageId: string,
  ) => MessageVersion[]
}

export const useSessionStore = create<SessionState>()((set, get) => ({
  sessions: [],
  activeSessionId: null,
  messages: {},
  isLoading: false,
  deletingSessionIds: new Set<string>(),
  error: null,
  wsStatus: WebSocketStatus.DISCONNECTED,
  forceReconnect: false,
  messagePagination: {},
  _wsUnsubscribers: null,

  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => {
    set((state) => {
      const sessionMessages = state.messages[sessionId] || []

      /**
       * BUG-FIX-fix_20260507_duplicate_messages:
       * 精确 ID 匹配 + 模糊匹配（角色 + 时间戳）
       */
      let messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      // 精确匹配失败时，如果 updates 中有 timestamp，尝试模糊匹配
      if (messageIndex < 0 && updates.role === 'assistant' && updates.timestamp) {
        messageIndex = sessionMessages.findIndex((m) =>
          m.role === 'assistant'
          && m.timestamp === updates.timestamp,
        )
      }

      if (messageIndex < 0) {
        logger.warn('消息不存在，跳过更新:', messageId)
        return state
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        ...updates,
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  fetchMessages: async (
    sessionId: string,
    options?: { limit?: number; before_sequence?: number; append?: boolean },
  ) => {
    try {
      const limit = options?.limit ?? 20
      const beforeSequence = options?.before_sequence
      const append = options?.append ?? false

      logger.debug(
        '开始从数据库加载执行记录 | sessionId:',
        sessionId,
        'limit:',
        limit,
        'before_sequence:',
        beforeSequence,
        'append:',
        append,
      )

      if (sessionId.startsWith('temp-')) {
        logger.debug('跳过临时会话的消息加载 | sessionId:', sessionId)
        set((state) => ({
          messages: {
            ...state.messages,
            [sessionId]: [],
          },
          messagePagination: {
            ...state.messagePagination,
            [sessionId]: {
              hasMore: false,
              oldestSequence: null,
              limit: 20,
              isLoadingMore: false,
            },
          },
        }))
        return
      }

      if (append) {
        set((state) => ({
          messagePagination: {
            ...state.messagePagination,
            [sessionId]: {
              ...state.messagePagination[sessionId],
              isLoadingMore: true,
            },
          },
        }))
      }

      const apiResult = await sessionApi.getMessages(sessionId, {
        limit,
        before_sequence: beforeSequence,
      })

      const rawMessages: Message[] = apiResult.messages
      const hasMore = apiResult.has_more

      // 计算最旧消息的 sequence（用于游标分页）
      const oldestSequence = rawMessages.length > 0
        ? Math.min(...rawMessages.map((m) => m.sequence ?? Number.MAX_SAFE_INTEGER))
        : null

      logger.debug(
        '成功加载执行记录 | sessionId:',
        sessionId,
        'count:',
        rawMessages.length,
        'hasMore:',
        hasMore,
        'oldestSequence:',
        oldestSequence,
      )

      set((state) => {
        const existingMessages = state.messages[sessionId] || []
        let updatedMessages: Message[]

        if (append) {
          updatedMessages = [...rawMessages, ...existingMessages]
        } else {
          /**
           * BUG-FIX-fix_20260507_duplicate_messages:
           * 问题根因: WebSocket stream_start 的 message_id (UUID格式如 550e8400-e29b-41d4-...)
           *          与 API 返回的 record_id (12位hex如 550e8400e29b) 格式不一致，
           *          导致按 ID 去重失败，同一条消息出现两次。
           * 修复方案: 首次加载时，通过「角色 + 时间戳」进行模糊匹配去重，
           *          而非仅依赖 ID 精确匹配。
           */
          const apiIds = new Set(rawMessages.map((m) => m.id))

          // 构建已知的 API 消息指纹集合（用于模糊匹配）
          const apiFingerprints = new Set(
            rawMessages.map((m) => `${m.role}::${m.timestamp}`),
          )

          const localOnly = existingMessages.filter((m) => {
            // 精确 ID 匹配：排除 API 已包含的
            if (apiIds.has(m.id)) return false
            // 模糊匹配：同角色 + 同时间戳的消息视为同一条
            const fingerprint = `${m.role}::${m.timestamp}`
            if (apiFingerprints.has(fingerprint)) return false
            return true
          })

          updatedMessages = localOnly.length > 0 ? [...rawMessages, ...localOnly] : rawMessages
        }

        updatedMessages.sort((a, b) => {
          const seqA = a.sequence ?? Number.MAX_SAFE_INTEGER
          const seqB = b.sequence ?? Number.MAX_SAFE_INTEGER
          if (seqA !== seqB) return seqA - seqB
          return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
        })

        return {
          messages: {
            ...state.messages,
            [sessionId]: updatedMessages,
          },
          messagePagination: {
            ...state.messagePagination,
            [sessionId]: {
              hasMore,
              oldestSequence,
              limit,
              isLoadingMore: false,
            },
          },
        }
      })
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : '未知错误'
      set((state) => ({
        messagePagination: {
          ...state.messagePagination,
          [sessionId]: {
            ...state.messagePagination[sessionId],
            isLoadingMore: false,
          },
        },
      }))

      console.error(
        '[sessionStore.fetchMessages] 从数据库加载执行记录失败 | sessionId:',
        sessionId,
        'error:',
        error,
      )
      throw new Error(`从数据库加载执行记录失败: ${message}`)
    }
  },

  fetchSubMessages: async (sessionId: string, parentId: string) => {
    try {
      const existingMessages = get().messages[sessionId] || []
      const hasSubMessages = existingMessages.some((m) => m.parentId === parentId)

      if (hasSubMessages) {
        logger.debug('子消息已加载，跳过 | parentId:', parentId)
        return
      }

      logger.debug('加载子消息 | sessionId:', sessionId, 'parentId:', parentId)

      const subResult = await getMessages(sessionId, {
        parentId,
        limit: 100,
      })

      const subMessages = subResult.messages
      logger.debug('子消息加载完成 | parentId:', parentId, 'count:', subMessages.length)

      set((state) => {
        const currentMessages = state.messages[sessionId] || []
        const newIds = new Set(subMessages.map((m) => m.id))
        const merged = [...currentMessages.filter((m) => !newIds.has(m.id)), ...subMessages]

        return {
          messages: {
            ...state.messages,
            [sessionId]: merged,
          },
        }
      })
    } catch (error: unknown) {
      console.error(
        '[sessionStore.fetchSubMessages] 加载子消息失败 | sessionId:',
        sessionId,
        'parentId:',
        parentId,
        'error:',
        error,
      )
    }
  },

  loadMoreMessages: async (sessionId: string) => {
    const state = get()
    const pagination = state.messagePagination[sessionId]

    if (!pagination || pagination.isLoadingMore || !pagination.hasMore) {
      logger.debug(
        '跳过加载 | sessionId:',
        sessionId,
        'reason:',
        !pagination
          ? 'no pagination'
          : pagination.isLoadingMore
            ? 'already loading'
            : 'no more messages',
      )
      return
    }

    logger.debug(
      '加载更多历史消息 | sessionId:',
      sessionId,
      'before_sequence:',
      pagination.oldestSequence,
      'limit:',
      pagination.limit,
    )

    await get().fetchMessages(sessionId, {
      before_sequence: pagination.oldestSequence ?? undefined,
      limit: pagination.limit,
      append: true,
    })
  },

  getMessagePagination: (sessionId: string) => {
    const state = get()
    return (
      state.messagePagination[sessionId] ?? {
        hasMore: true,
        oldestSequence: null,
        limit: 20,
        isLoadingMore: false,
      }
    )
  },

  connectWebSocket: (sessionId: string, token: string) => {
    const { _wsUnsubscribers: prevUnsubscribers } = get()
    if (prevUnsubscribers) {
      prevUnsubscribers.cleanup()
    }

    wsPool.connect(sessionId, token)
    wsPool.setActiveThread(sessionId)

    set({ wsStatus: wsPool.getStatus() })
  },

  disconnectWebSocket: () => {
    const { activeSessionId } = get()
    if (activeSessionId) {
      wsPool.disconnect(activeSessionId)
    }
    set({ wsStatus: WebSocketStatus.DISCONNECTED, _wsUnsubscribers: null })
  },

  clearError: () => {
    set({ error: null })
  },

  retryMessage: async (
    sessionId: string,
    messageId: string,
    scope: RetryScope = 'all',
    targetToolId?: string,
  ) => {
    const state = get()
    const sessionMessages = state.messages[sessionId] || []
    const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

    if (messageIndex < 0) {
      logger.warn('消息不存在:', messageId)
      return
    }

    const message = sessionMessages[messageIndex]

    useMessageVersionStore.getState().createVersion(message)

    if (scope === 'all') {
      set((state) => {
        const sessionMessages = state.messages[sessionId] || []
        const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

        if (messageIndex < 0) {
          return state
        }

        const updatedMessages = [...sessionMessages]
        updatedMessages[messageIndex] = {
          ...updatedMessages[messageIndex],
          content: '',
          timestamp: new Date().toISOString(),
          toolCalls: [],
          thinking: undefined,
        }

        return {
          messages: {
            ...state.messages,
            [sessionId]: updatedMessages,
          },
        }
      })
    } else if (scope === 'failed_tools') {
      const failedToolCalls = message.toolCalls?.filter((tc) => tc.status === 'failed') || []

      for (const toolCall of failedToolCalls) {
        get().updateMessageFields(sessionId, messageId, {
          toolCalls: (get().messages[sessionId] || [])
            .find((m) => m.id === messageId)
            ?.toolCalls?.map((tc) =>
              tc.call_id === toolCall.call_id
                ? { ...tc, status: 'pending' as const, error: undefined, result: undefined }
                : tc,
            ),
        })
      }
    } else if (scope === 'specific_tool') {
      if (!targetToolId) {
        logger.warn('scope="specific_tool" 但未提供 targetToolId')
        return
      }

      get().updateMessageFields(sessionId, messageId, {
        toolCalls: (get().messages[sessionId] || [])
          .find((m) => m.id === messageId)
          ?.toolCalls?.map((tc) =>
            tc.call_id === targetToolId
              ? { ...tc, status: 'pending' as const, error: undefined, result: undefined }
              : tc,
          ),
      })
    }

    try {
      await messageApi.retryMessage(sessionId, messageId)
    } catch (error) {
      const errorMsg = error instanceof Error ? error.message : String(error)
      console.error('[sessionStore.retryMessage] 重试失败:', errorMsg, error)
      throw error
    }
  },

  createMessageVersion: (sessionId: string, messageId: string) => {
    const state = get()
    const sessionMessages = state.messages[sessionId] || []
    const message = sessionMessages.find((m) => m.id === messageId)

    if (!message) {
      logger.warn('消息不存在:', messageId)
      return 0
    }

    return useMessageVersionStore.getState().createVersion(message)
  },

  restoreMessageVersion: (sessionId: string, messageId: string, version: number) => {
    const restoredMessage = useMessageVersionStore.getState().restoreVersion(messageId, version)

    if (!restoredMessage) {
      logger.warn('恢复版本失败')
      return
    }

    set((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        return state
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = restoredMessage

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  getMessageVersions: (_sessionId: string, messageId: string) => {
    return useMessageVersionStore.getState().getVersions(messageId)
  },
}))
