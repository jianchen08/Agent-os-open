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
import { webSocketService, WebSocketStatus } from '@/services/websocket/WebSocketService'
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
      const wsStatus = webSocketService.getStatus()
      const isConnected = webSocketService.isConnected()
      const quality = webSocketService.getNetworkQuality()

      // Try to get queue status
      let queuedMessages = 0
      try {
        const queueStatus = await webSocketService.getQueueStatus()
        queuedMessages = queueStatus.pending + queueStatus.failed
      } catch {
        // Queue might not be initialized
      }

      // Try to get performance stats for latency
      let latencyMs: number | null = null
      try {
        const stats = await webSocketService.getPerformanceStats()
        if (stats.heartbeat?.lastRttMs !== undefined) {
          latencyMs = Math.round(stats.heartbeat.lastRttMs)
        }
      } catch {
        // Stats might not be available
      }

      updateConnectionStatus({
        state: mapStatus(wsStatus),
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
    webSocketService.subscribe('connect', handleConnect)
    webSocketService.subscribe('disconnect', handleDisconnect)
    webSocketService.subscribe('error', handleError)
    webSocketService.subscribe('auth_error', handleAuthError)
    webSocketService.subscribe('network_quality_change', handleNetworkQualityChange)

    // Initial sync
    syncStatus()

    // Periodic polling for connection quality (every 5 seconds)
    const pollInterval = setInterval(syncStatus, 5000)

    // Keep a ref to the latest latency for the quality callback
    let latencyMs: number | null = null

    return () => {
      webSocketService.unsubscribe('connect', handleConnect)
      webSocketService.unsubscribe('disconnect', handleDisconnect)
      webSocketService.unsubscribe('error', handleError)
      webSocketService.unsubscribe('auth_error', handleAuthError)
      webSocketService.unsubscribe('network_quality_change', handleNetworkQualityChange)
      clearInterval(pollInterval)
    }
  }, [updateConnectionStatus])
}
