import { create } from 'zustand'
import { messageApi } from '@/services/api/messages'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WebSocketStatus } from '@/services/websocket/WebSocketService'
import { loggers } from '@/utils/logger'
import { useMessageVersionStore } from './messageVersionStore'
import { usePipelineMessageStore } from './pipelineMessageStore'
import type { MessageVersion } from './messageVersionStore'
import type { Message, RetryScope, Session } from '@/types/models'

const logger = loggers.sessionStore

interface SessionState {
  sessions: Session[]
  activeSessionId: string | null
  isLoading: boolean
  deletingSessionIds: Set<string>
  error: string | null
  wsStatus: string
  forceReconnect: boolean
  _wsUnsubscribers: { cleanup: () => void } | null

  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => void
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
  isLoading: false,
  deletingSessionIds: new Set<string>(),
  error: null,
  wsStatus: WebSocketStatus.DISCONNECTED,
  forceReconnect: false,
  _wsUnsubscribers: null,

  updateMessageFields: (sessionId: string, messageId: string, updates: Partial<Message>) => {
    usePipelineMessageStore.getState().updateMessage(sessionId, messageId, updates)
  },

  connectWebSocket: (sessionId: string, token: string) => {
    const { _wsUnsubscribers: prevUnsubscribers } = get()
    if (prevUnsubscribers) {
      prevUnsubscribers.cleanup()
    }

    globalWS.connect(token)

    set({ wsStatus: globalWS.status === 'connected' ? WebSocketStatus.CONNECTED : WebSocketStatus.CONNECTING })
  },

  disconnectWebSocket: () => {
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
    const pipelineStore = usePipelineMessageStore.getState()
    const pipelineMessages = pipelineStore.getMessages(sessionId)
    const message = pipelineMessages.find((m) => m.id === messageId)

    if (!message) {
      logger.warn('消息不存在:', messageId)
      return
    }

    useMessageVersionStore.getState().createVersion(message)

    if (scope === 'all') {
      pipelineStore.updateMessage(sessionId, messageId, {
        content: '',
        timestamp: new Date().toISOString(),
        toolCalls: [],
        thinking: undefined,
      })
    } else if (scope === 'failed_tools') {
      const failedToolCalls = message.toolCalls?.filter((tc) => tc.status === 'failed') || []

      for (const toolCall of failedToolCalls) {
        const currentMessage = pipelineStore.getMessages(sessionId).find((m) => m.id === messageId)
        pipelineStore.updateMessage(sessionId, messageId, {
          toolCalls: currentMessage?.toolCalls?.map((tc) =>
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

      const currentMessage = pipelineStore.getMessages(sessionId).find((m) => m.id === messageId)
      pipelineStore.updateMessage(sessionId, messageId, {
        toolCalls: currentMessage?.toolCalls?.map((tc) =>
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
    const pipelineMessages = usePipelineMessageStore.getState().getMessages(sessionId)
    const message = pipelineMessages.find((m) => m.id === messageId)

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

    usePipelineMessageStore.getState().updateMessage(sessionId, messageId, restoredMessage)
  },

  getMessageVersions: (_sessionId: string, messageId: string) => {
    return useMessageVersionStore.getState().getVersions(messageId)
  },
}))
