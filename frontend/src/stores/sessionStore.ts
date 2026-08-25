/**
 * 会话会话级 UI 状态 store（服务端状态 query 化瘦身）
 *
 * sessions 列表数据已迁 TanStack Query（hooks/queries/useSessionsQuery），
 * 本 store 只保留纯客户端状态：当前选中会话、删除中标记、WS 连接状态。
 */

import { create } from 'zustand'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { WebSocketStatus } from '@/constants/websocket'

interface SessionState {
  activeSessionId: string | null
  deletingSessionIds: Set<string>
  wsStatus: string
  forceReconnect: boolean
  _wsUnsubscribers: { cleanup: () => void } | null

  connectWebSocket: (token: string) => void
  disconnectWebSocket: () => void
}

export const useSessionStore = create<SessionState>()((set, get) => ({
  activeSessionId: null,
  deletingSessionIds: new Set<string>(),
  wsStatus: WebSocketStatus.DISCONNECTED,
  forceReconnect: false,
  _wsUnsubscribers: null,

  connectWebSocket: (token: string) => {
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
}))
