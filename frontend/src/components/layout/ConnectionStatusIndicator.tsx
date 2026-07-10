/**
 * Connection Status Indicator Component
 *
 * Displays the current WebSocket connection status with visual feedback.
 * Shows connection state, latency, reconnect attempts, and queued messages.
 */

import React, { useState, useCallback } from 'react'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { ConnectionStatus } from '@/stores/layoutModeStore'

/** Status display configuration */
const STATUS_CONFIG: Record<
  ConnectionStatus['state'],
  { color: string; label: string; bgColor: string; animation: string }
> = {
  connected: {
    color: 'text-status-success',
    label: 'Connected',
    bgColor: 'bg-[var(--badge-success-bg)]',
    animation: '',
  },
  connecting: {
    color: 'text-[var(--badge-warning-text)]',
    label: 'Connecting...',
    bgColor: 'bg-[var(--badge-warning-bg)]',
    animation: 'animate-pulse',
  },
  reconnecting: {
    color: 'text-status-running',
    label: 'Reconnecting...',
    bgColor: 'bg-[var(--badge-info-bg)]',
    animation: 'animate-pulse',
  },
  disconnected: {
    color: 'text-status-pending',
    label: 'Disconnected',
    bgColor: 'bg-[var(--badge-default-bg)]',
    animation: '',
  },
  failed: {
    color: 'text-[var(--badge-error-text)]',
    label: 'Connection Failed',
    bgColor: 'bg-[var(--badge-error-bg)]',
    animation: '',
  },
}

interface ConnectionStatusIndicatorProps {
  /** Compact mode - only show the dot indicator */
  compact?: boolean
  /** Show latency info */
  showLatency?: boolean
  /** Show queued messages count */
  showQueue?: boolean
  /** Additional CSS class */
  className?: string
}

/**
 * Connection Status Indicator Component
 *
 * Renders a visual indicator showing the WebSocket connection status.
 * Supports compact mode (dot only) and expanded mode (with labels and details).
 */
export function ConnectionStatusIndicator({
  compact = false,
  showLatency = true,
  showQueue = true,
  className = '',
}: ConnectionStatusIndicatorProps) {
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)
  const [showDetails, setShowDetails] = useState(false)

  const config = STATUS_CONFIG[connectionStatus.state]

  const toggleDetails = useCallback(() => setShowDetails((prev) => !prev), [])

  if (compact) {
    return (
      <button
        onClick={toggleDetails}
        className={`relative inline-flex items-center ${className}`}
        title={`${config.label}${connectionStatus.latencyMs ? ` (${connectionStatus.latencyMs}ms)` : ''}`}
      >
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            connectionStatus.state === 'connected'
              ? 'bg-status-success'
              : connectionStatus.state === 'failed'
                ? 'bg-status-error'
                : connectionStatus.state === 'connecting' || connectionStatus.state === 'reconnecting'
                  ? 'bg-status-warning animate-pulse'
                  : 'bg-status-pending'
          }`}
        />
        {connectionStatus.queuedMessages > 0 && (
          <span className="absolute -top-1 -right-1 flex h-3 w-3 items-center justify-center rounded-full bg-status-running text-[8px] font-bold text-white">
            {connectionStatus.queuedMessages > 9 ? '9+' : connectionStatus.queuedMessages}
          </span>
        )}
      </button>
    )
  }

  return (
    <div className={`relative ${className}`}>
      <button
        onClick={toggleDetails}
        className={`flex items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors ${config.bgColor} ${config.color} hover:opacity-80`}
      >
        {/* Status dot */}
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            connectionStatus.state === 'connected'
              ? 'bg-status-success'
              : connectionStatus.state === 'failed'
                ? 'bg-status-error'
                : connectionStatus.state === 'connecting' ||
                    connectionStatus.state === 'reconnecting'
                  ? 'bg-status-warning animate-pulse'
                  : 'bg-status-pending'
          } ${config.animation}`}
        />

        {/* Status label */}
        <span className="font-medium">{config.label}</span>

        {/* Latency indicator */}
        {showLatency && connectionStatus.latencyMs !== null && (
          <span className="text-[10px] opacity-70">
            {connectionStatus.latencyMs < 50
              ? 'Fast'
              : connectionStatus.latencyMs < 200
                ? `${connectionStatus.latencyMs}ms`
                : `${connectionStatus.latencyMs}ms`}
          </span>
        )}

        {/* Queue indicator */}
        {showQueue && connectionStatus.queuedMessages > 0 && (
          <span className="flex h-4 min-w-[16px] items-center justify-center rounded-full bg-[var(--badge-info-bg)] px-1 text-[10px] font-bold">
            {connectionStatus.queuedMessages}
          </span>
        )}
      </button>

      {/* Expanded details popup */}
      {showDetails && (
        <div className="border-border bg-background absolute right-0 top-full z-50 mt-1 w-56 rounded-lg border p-3 shadow-lg">
          <h4 className="text-foreground mb-2 text-xs font-semibold">Connection Details</h4>

          <div className="space-y-1.5 text-xs">
            <div className="flex items-center justify-between">
              <span className="text-muted-foreground">Status</span>
              <span className={config.color}>{config.label}</span>
            </div>

            {connectionStatus.latencyMs !== null && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Latency</span>
                <span className={connectionStatus.latencyMs < 100 ? 'text-status-success' : connectionStatus.latencyMs < 300 ? 'text-status-warning' : 'text-status-error'}>
                  {connectionStatus.latencyMs}ms
                </span>
              </div>
            )}

            {connectionStatus.reconnectAttempt > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Retry</span>
                <span className="text-status-warning">Attempt {connectionStatus.reconnectAttempt}</span>
              </div>
            )}

            {connectionStatus.lastConnectedAt && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Last connected</span>
                <span className="text-muted-foreground">
                  {new Date(connectionStatus.lastConnectedAt).toLocaleTimeString()}
                </span>
              </div>
            )}

            {connectionStatus.queuedMessages > 0 && (
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">Queued</span>
                <span className="text-status-running">{connectionStatus.queuedMessages} messages</span>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
