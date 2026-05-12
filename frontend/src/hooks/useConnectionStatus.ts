/**
 * useConnectionStatus Hook
 *
 * 基于 GlobalWebSocket 单连接模式同步连接状态到 layout store。
 * 已从 WebSocketConnectionPool 迁移，不再依赖连接池。
 */

import { useEffect } from 'react'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

export function useConnectionStatus(): void {
  const updateConnectionStatus = useLayoutModeStore((s) => s.updateConnectionStatus)

  useEffect(() => {
    const handleGlobalStatus = (data: { status: string }) => {
      if (data.status === 'connected') {
        updateConnectionStatus({
          state: 'connected',
          lastConnectedAt: new Date().toISOString(),
          reconnectAttempt: 0,
        })
      } else if (data.status === 'disconnected') {
        updateConnectionStatus({
          state: 'disconnected',
        })
      }
    }

    globalWS.subscribe('_status', handleGlobalStatus)

    if (globalWS.status === 'connected') {
      updateConnectionStatus({
        state: 'connected',
        lastConnectedAt: new Date().toISOString(),
      })
    } else {
      updateConnectionStatus({
        state: globalWS.status === 'connecting' ? 'connecting' : 'disconnected',
      })
    }

    return () => {
      globalWS.unsubscribe('_status', handleGlobalStatus)
    }
  }, [updateConnectionStatus])
}
