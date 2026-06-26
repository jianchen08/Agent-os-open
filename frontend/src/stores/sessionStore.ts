import { create } from 'zustand'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WebSocketStatus } from '@/constants/websocket'
import { loggers } from '@/utils/logger'
import type { Session } from '@/types/models'

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

  connectWebSocket: (sessionId: string, token: string) => void
  disconnectWebSocket: () => void
  clearError: () => void
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
}))
