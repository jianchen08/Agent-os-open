/**
 * useConnectionStatus Hook
 *
 * Syncs the WebSocket service connection status into the layout mode store
 * so the ConnectionStatusIndicator can display reactive status updates.
 *
 * Usage:
 *   useConnectionStatus() // call once in a top-level component
 */

import { useEffect } from 'react'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { WebSocketStatus } from '@/services/websocket/WebSocketService'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { ConnectionStatus } from '@/stores/layoutModeStore'

/** Maps WebSocketService status to ConnectionStatus state */
function mapStatus(status: string): ConnectionStatus['state'] {
  switch (status) {
    case WebSocketStatus.CONNECTED:
      return 'connected'
    case WebSocketStatus.CONNECTING:
      return 'connecting'
    case WebSocketStatus.RECONNECTING:
      return 'reconnecting'
    case WebSocketStatus.FAILED:
      return 'failed'
    case WebSocketStatus.DISCONNECTED:
    default:
      return 'disconnected'
  }
}

/**
 * Hook to sync WebSocket connection status into the layout store.
 *
 * Subscribes to WebSocket connect/disconnect/error events and periodically
 * updates the connection status including latency and queue depth.
 */
export function useConnectionStatus(): void {
  const updateConnectionStatus = useLayoutModeStore((s) => s.updateConnectionStatus)

  useEffect(() => {
    /**
     * Poll connection status from the WebSocket service singleton
     */
    const syncStatus = async () => {
      const wsStatus = wsPool.getStatus()
      const isConnected = wsPool.hasAnyConnection() || globalWS.status === 'connected'
      const activeThread = wsPool.getActiveThread()
      const quality = activeThread ? wsPool.getNetworkQuality(activeThread) : 'unknown'

      const queuedMessages = 0
      let latencyMs: number | null = null

      if (activeThread) {
        try {
          const stats = await wsPool.getPerformanceStats(activeThread)
          if (stats?.heartbeat?.lastRttMs !== undefined) {
            latencyMs = Math.round(stats.heartbeat.lastRttMs)
          }
        } catch {
          // Stats might not be available
        }
      }

      const effectiveState = globalWS.status === 'connected' ? 'connected' : mapStatus(wsStatus)
      updateConnectionStatus({
        state: effectiveState,
        latencyMs,
        queuedMessages,
        lastConnectedAt: isConnected ? new Date().toISOString() : undefined,
      })
    }

    // Subscribe to WebSocket lifecycle events
    const handleConnect = () => {
      updateConnectionStatus({
        state: 'connected',
        lastConnectedAt: new Date().toISOString(),
        reconnectAttempt: 0,
      })
    }

    const handleDisconnect = () => {
      updateConnectionStatus({
        state: 'disconnected',
      })
    }

    const handleError = () => {
      updateConnectionStatus({
        state: 'failed',
      })
    }

    const handleAuthError = () => {
      updateConnectionStatus({
        state: 'failed',
      })
    }

    const handleNetworkQualityChange = (data: { quality: string } | unknown) => {
      // Extract RTT from network quality if available
      const qualityData = data as { quality: string }
      if (qualityData?.quality === 'excellent') {
        updateConnectionStatus({ latencyMs: latencyMs ?? null })
      }
    }

    // Register subscriptions
    wsPool.subscribe('connect', handleConnect)
    wsPool.subscribe('disconnect', handleDisconnect)
    wsPool.subscribe('error', handleError)
    wsPool.subscribe('auth_error', handleAuthError)
    wsPool.subscribe('network_quality_change', handleNetworkQualityChange)

    // Also subscribe to GlobalWebSocket status changes
    const handleGlobalStatus = (data: { status: string }) => {
      if (data.status === 'connected') {
        handleConnect()
      } else if (data.status === 'disconnected') {
        handleDisconnect()
      }
    }
    globalWS.subscribe('_status', handleGlobalStatus)

    // Initial sync — also consider globalWS status
    const poolStatus = wsPool.getStatus()
    const globalStatus = globalWS.status
    if (globalStatus === 'connected' || poolStatus === WebSocketStatus.CONNECTED) {
      handleConnect()
    }

    // Periodic polling for connection quality (every 5 seconds)
    const pollInterval = setInterval(syncStatus, 5000)

    // Keep a ref to the latest latency for the quality callback
    const latencyMs: number | null = null

    return () => {
      wsPool.unsubscribe('connect', handleConnect)
      wsPool.unsubscribe('disconnect', handleDisconnect)
      wsPool.unsubscribe('error', handleError)
      wsPool.unsubscribe('auth_error', handleAuthError)
      wsPool.unsubscribe('network_quality_change', handleNetworkQualityChange)
      globalWS.unsubscribe('_status', handleGlobalStatus)
      clearInterval(pollInterval)
    }
  }, [updateConnectionStatus])
}
