import { create } from 'zustand'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { messageApi } from '@/services/api/messages'
import { getMessages } from '@/services/api/session'
import { WebSocketStatus, webSocketService } from '@/services/websocket/WebSocketService'
import { loggers } from '@/utils/logger'
import { useMessageVersionStore } from './messageVersionStore'
import type { Message, RetryScope } from '@/types/models'

const logger = loggers.sessionStore

const sessionApi = {
  getMessages,
}

interface MessagePaginationState {
  hasMore: boolean
  offset: number
  limit: number
  isLoadingMore: boolean
}

interface SessionState {
  sessions: import('../types/models').Session[]
  activeSessionId: string | null
  messages: Record<string, Message[]>
  isLoading: boolean
  deletingSessionIds: Set<string>
  error: string | null
  wsStatus: string
  forceReconnect: boolean
  messagePagination: Record<string, MessagePaginationState>
  _wsUnsubscribers: { cleanup: () => void } | null

  addMessage: (sessionId: string, message: Message) => void
  updateMessageContent: (
    sessionId: string,
    messageId: string,
    content: string,
    options?: { mode?: 'append' | 'replace' },
  ) => void
  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => void
  deleteMessage: (sessionId: string, messageId: string, includeTarget?: boolean) => void
  deleteMessageFromList: (sessionId: string, messageId: string, deletedCount?: number) => void
  getActiveSessionMessages: () => Message[]
  setMessages: (sessionId: string, messages: Message[]) => void
  fetchMessages: (
    sessionId: string,
    options?: { skip?: number; limit?: number; append?: boolean },
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
  ) => import('./messageVersionStore').MessageVersion[]
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

  addMessage: (sessionId: string, message: Message) => {
    set((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const realMessageId = (message as Message & { message_id?: string }).message_id || message.id

      const existingIndex = sessionMessages.findIndex((m) => m.id === realMessageId)

      let updatedMessages: Message[]
      let messageCountChanged = false

      if (existingIndex >= 0) {
        updatedMessages = [...sessionMessages]
        updatedMessages[existingIndex] = {
          ...sessionMessages[existingIndex],
          ...message,
          id: realMessageId,
        }
      } else {
        updatedMessages = [
          ...sessionMessages,
          {
            ...message,
            id: realMessageId,
          },
        ]
        messageCountChanged = true
      }

      updatedMessages.sort((a, b) => {
        const seqA = a.sequence ?? Number.MAX_SAFE_INTEGER
        const seqB = b.sequence ?? Number.MAX_SAFE_INTEGER
        if (seqA !== seqB) {
          return seqA - seqB
        }
        return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      })

      const newMessages = {
        ...state.messages,
        [sessionId]: updatedMessages,
      }

      const newSessions = messageCountChanged
        ? state.sessions.map((session) => {
            if (session.id === sessionId) {
              return {
                ...session,
                messageCount: session.messageCount + 1,
                updatedAt: new Date().toISOString(),
              }
            }
            return session
          })
        : state.sessions

      return {
        messages: newMessages,
        sessions: newSessions,
      }
    })
  },

  updateMessageContent: (
    sessionId: string,
    messageId: string,
    content: string,
    options?: { mode?: 'append' | 'replace' },
  ) => {
    const mode = options?.mode ?? 'append'

    set((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        logger.warn('消息不存在，跳过更新:', messageId)
        return state
      }

      const oldContent = sessionMessages[messageIndex].content || ''
      let newContent: string

      if (mode === 'replace') {
        newContent = content
      } else {
        newContent = oldContent + content
      }

      const updatedMessages = [...sessionMessages]
      updatedMessages[messageIndex] = {
        ...updatedMessages[messageIndex],
        content: newContent,
        timestamp:
          mode === 'replace' ? new Date().toISOString() : updatedMessages[messageIndex].timestamp,
      }

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
      }
    })
  },

  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => {
    set((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

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

  deleteMessage: (sessionId: string, messageId: string, includeTarget: boolean = true) => {
    logger.debug('开始删除:', {
      sessionId,
      messageId,
      includeTarget,
    })

    const previousMessages = get().messages[sessionId] || []
    const previousSessions = get().sessions

    set((state) => {
      const sessionMessages = state.messages[sessionId] || []
      const targetMessage = sessionMessages.find((m) => m.id === messageId)

      logger.debug('查找结果:', {
        totalMessages: sessionMessages.length,
        targetMessage: targetMessage
          ? {
              id: targetMessage.id,
              sequence: targetMessage.sequence,
              parentId: targetMessage.parentId,
            }
          : null,
      })

      if (!targetMessage) {
        logger.warn('未找到要删除的消息')
        return state
      }

      const targetSequence = targetMessage.sequence || 0
      const targetParentId = targetMessage.parentId || null

      const mainRecordIds = new Set<string>()
      sessionMessages.forEach((m) => {
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

      const messageMap = new Map<string, typeof targetMessage>()
      sessionMessages.forEach((m) => messageMap.set(m.id, m))

      let currentParentIds = Array.from(allIdsToDelete)
      while (currentParentIds.length > 0) {
        const childrenIds: string[] = []
        sessionMessages.forEach((m) => {
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

      logger.debug('递归查找完成:', {
        主记录数: mainRecordIds.size,
        总删除数: allIdsToDelete.size,
        includeTarget,
      })

      const updatedMessages = sessionMessages.filter((m) => !allIdsToDelete.has(m.id))
      const deletedCount = sessionMessages.length - updatedMessages.length

      logger.debug('删除结果:', {
        原始数量: sessionMessages.length,
        删除后数量: updatedMessages.length,
        删除的消息数: deletedCount,
        targetSequence,
        targetParentId,
      })

      const newSessions = state.sessions.map((session) => {
        if (session.id === sessionId) {
          return {
            ...session,
            messageCount: Math.max(0, session.messageCount - deletedCount),
            updatedAt: new Date().toISOString(),
          }
        }
        return session
      })

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
        sessions: newSessions,
      }
    })

    logger.debug('调用后端 API 删除')
    messageApi
      .deleteMessage(sessionId, messageId, includeTarget)
      .then(async (result) => {
        logger.debug('后端删除成功:', result)
        try {
          const messages = await sessionApi.getMessages(sessionId)
          logger.debug('重新加载消息成功:', messages.length)
          set((state) => ({
            messages: {
              ...state.messages,
              [sessionId]: messages,
            },
          }))
        } catch (reloadError) {
          logger.warn('重新加载消息失败，保持乐观更新状态:', reloadError)
        }
      })
      .catch((error) => {
        if (error.code === '404') {
          logger.warn('消息已被删除，保持前端状态')
          return
        }

        console.error('删除消息失败，回滚前端状态:', error)

        set((state) => ({
          messages: {
            ...state.messages,
            [sessionId]: previousMessages,
          },
          sessions: previousSessions,
        }))

        throw error
      })
  },

  deleteMessageFromList: (sessionId: string, messageId: string, deletedCount?: number) => {
    logger.debug('开始删除:', {
      sessionId,
      messageId,
      deletedCount,
    })

    set((state) => {
      const sessionMessages = state.messages[sessionId] || []

      const messageIndex = sessionMessages.findIndex((m) => m.id === messageId)

      if (messageIndex < 0) {
        logger.warn('未找到消息:', messageId)
        return state
      }

      let updatedMessages: Message[]
      if (deletedCount && deletedCount > 0) {
        updatedMessages = [
          ...sessionMessages.slice(0, messageIndex),
          ...sessionMessages.slice(messageIndex + deletedCount),
        ]
      } else {
        updatedMessages = sessionMessages.slice(0, messageIndex)
      }

      logger.debug('删除结果:', {
        原始数量: sessionMessages.length,
        删除后数量: updatedMessages.length,
        删除的消息数: sessionMessages.length - updatedMessages.length,
      })

      const updatedSessions = state.sessions.map((s) => {
        if (s.id === sessionId) {
          return {
            ...s,
            messageCount: updatedMessages.length,
            updatedAt: new Date().toISOString(),
          }
        }
        return s
      })

      return {
        messages: {
          ...state.messages,
          [sessionId]: updatedMessages,
        },
        sessions: updatedSessions,
      }
    })
  },

  getActiveSessionMessages: () => {
    const { activeSessionId, messages } = get()

    if (!activeSessionId) {
      return []
    }

    return messages[activeSessionId] || []
  },

  setMessages: (sessionId: string, messages: Message[]) => {
    set((state) => ({
      messages: {
        ...state.messages,
        [sessionId]: messages,
      },
    }))
  },

  fetchMessages: async (
    sessionId: string,
    options?: { skip?: number; limit?: number; append?: boolean },
  ) => {
    try {
      const limit = options?.limit ?? 20
      const skip = options?.skip ?? 0
      const append = options?.append ?? false

      logger.debug(
        '开始从数据库加载执行记录 | sessionId:',
        sessionId,
        'skip:',
        skip,
        'limit:',
        limit,
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
              offset: 0,
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

      const apiMessages = await sessionApi.getMessages(sessionId, {
        skip,
        limit,
      })

      const rawMessages: Message[] = apiMessages

      const hasMore = rawMessages.length === limit

      logger.debug(
        '成功加载执行记录 | sessionId:',
        sessionId,
        'count:',
        rawMessages.length,
        'hasMore:',
        hasMore,
      )

      set((state) => {
        const existingMessages = state.messages[sessionId] || []
        let updatedMessages: Message[]

        if (append) {
          updatedMessages = [...rawMessages, ...existingMessages]
        } else {
          // Merge: keep local-only messages (e.g. optimistically added user messages)
          // that aren't in the API response yet
          const apiIds = new Set(rawMessages.map((m) => m.id))
          const localOnly = existingMessages.filter((m) => !apiIds.has(m.id))
          updatedMessages = localOnly.length > 0 ? [...rawMessages, ...localOnly] : rawMessages
        }

        return {
          messages: {
            ...state.messages,
            [sessionId]: updatedMessages,
          },
          messagePagination: {
            ...state.messagePagination,
            [sessionId]: {
              hasMore,
              offset: skip + rawMessages.length,
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

      const subMessages = await getMessages(sessionId, {
        parentId,
        limit: 100,
      })

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
      'offset:',
      pagination.offset,
      'limit:',
      pagination.limit,
    )

    await get().fetchMessages(sessionId, {
      skip: pagination.offset,
      limit: pagination.limit,
      append: true,
    })
  },

  getMessagePagination: (sessionId: string) => {
    const state = get()
    return (
      state.messagePagination[sessionId] ?? {
        hasMore: true,
        offset: 0,
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

    const handleStatusChange = () => {
      set({ wsStatus: webSocketService.getStatus() })
    }

    webSocketService.subscribe('connect', handleStatusChange)
    webSocketService.subscribe('disconnect', handleStatusChange)

    webSocketService.subscribe(WS_SERVER_EVENTS.STATE_CHANGE, (_data) => {})

    set({
      _wsUnsubscribers: {
        cleanup: () => {
          webSocketService.unsubscribe('connect', handleStatusChange)
          webSocketService.unsubscribe('disconnect', handleStatusChange)
        },
      },
    })

    webSocketService.connect(sessionId, token)
    set({ wsStatus: webSocketService.getStatus() })
  },

  disconnectWebSocket: () => {
    const { _wsUnsubscribers } = get()
    if (_wsUnsubscribers) {
      _wsUnsubscribers.cleanup()
    }
    webSocketService.disconnect()
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
