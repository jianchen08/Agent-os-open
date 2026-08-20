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
      // 「从未连接」（刷新后 token/JS 加载期，connect 尚未发起）≠「断开」：
      // 映射为 connecting，避免刷新瞬间就弹"内核连接已断开，正在尝试恢复…"横幅
      // （2026-08-20 回归：该横幅叠加 AlertBanner 的 4s 解除保留期，用户感知
      // 为"每次刷新要 5-10s 才连上后端"）。connect 发起过之后的断开才是真断开。
      const state =
        globalWS.status === 'connecting' || !globalWS.hasAttemptedConnect
          ? 'connecting'
          : 'disconnected'
      updateConnectionStatus({
        state,
      })
    }

    return () => {
      globalWS.unsubscribe('_status', handleGlobalStatus)
    }
  }, [updateConnectionStatus])
}
