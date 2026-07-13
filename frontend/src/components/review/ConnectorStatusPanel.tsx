/**
 * ConnectorStatusPanel - 外部软件连接器状态面板
 *
 * 显示已配置的外部创作软件连接器的连接状态，
 * 支持连接/断开操作。
 */

import { Plug, Unplug, RefreshCw, ExternalLink, Settings } from 'lucide-react'
import React, { useState, useCallback } from 'react'
import type { ExternalConnector, ConnectorStatus } from '@/types/review'

export interface ConnectorStatusPanelProps {
  /** 已配置的连接器列表 */
  connectors: ExternalConnector[]
  /** 连接回调 */
  onConnect?: (connectorId: string) => void
  /** 断开回调 */
  onDisconnect?: (connectorId: string) => void
  /** 刷新回调 */
  onRefresh?: (connectorId: string) => void
  /** 配置回调 */
  onConfigure?: (connectorId: string) => void
}

/** 状态颜色映射 */
const statusColors: Record<ConnectorStatus, string> = {
  connected: 'bg-green-500',
  connecting: 'bg-yellow-500 animate-pulse',
  disconnected: 'bg-gray-400',
  error: 'bg-red-500',
}

/** 状态文本映射 */
const statusLabels: Record<ConnectorStatus, string> = {
  connected: '已连接',
  connecting: '连接中...',
  disconnected: '未连接',
  error: '连接错误',
}

/** 连接器类型图标 */
const typeIcons: Record<string, string> = {
  comfyui: '🎨',
  game_engine: '🎮',
  video_editor: '🎬',
  generic: '🔌',
}

/**
 * ConnectorStatusPanel
 *
 * 展示外部创作软件的连接状态卡片列表。
 */
export function ConnectorStatusPanel({
  connectors,
  onConnect,
  onDisconnect,
  onRefresh,
  onConfigure,
}: ConnectorStatusPanelProps) {
  if (connectors.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center p-6 text-center">
        <Plug className="mb-2 h-8 w-8 text-muted-foreground/40" />
        <p className="text-sm text-muted-foreground">暂无外部软件连接</p>
        <p className="mt-1 text-xs text-muted-foreground/60">
          配置 ComfyUI、游戏引擎等外部创作软件以启用审批集成
        </p>
      </div>
    )
  }

  return (
    <div className="space-y-2 p-3">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-xs font-medium text-foreground">外部软件连接</span>
        <span className="text-[10px] text-muted-foreground">
          {connectors.filter((c) => c.status === 'connected').length}/{connectors.length} 已连接
        </span>
      </div>

      {connectors.map((connector) => (
        <ConnectorCard
          key={connector.id}
          connector={connector}
          onConnect={onConnect}
          onDisconnect={onDisconnect}
          onRefresh={onRefresh}
          onConfigure={onConfigure}
        />
      ))}
    </div>
  )
}

/** 单个连接器卡片 */
function ConnectorCard({
  connector,
  onConnect,
  onDisconnect,
  onRefresh,
  onConfigure,
}: {
  connector: ExternalConnector
  onConnect?: (id: string) => void
  onDisconnect?: (id: string) => void
  onRefresh?: (id: string) => void
  onConfigure?: (id: string) => void
}) {
  const [isOperating, setIsOperating] = useState(false)

  const handleToggle = useCallback(async () => {
    setIsOperating(true)
    try {
      if (connector.status === 'connected') {
        onDisconnect?.(connector.id)
      } else {
        onConnect?.(connector.id)
      }
    } finally {
      // 延迟重置操作状态
      setTimeout(() => setIsOperating(false), 500)
    }
  }, [connector.id, connector.status, onConnect, onDisconnect])

  const icon = typeIcons[connector.type] || typeIcons.generic

  return (
    <div className="flex items-center gap-3 rounded-lg border border-border bg-background p-2.5">
      {/* 图标 + 状态灯 */}
      <div className="relative">
        <span className="text-lg">{icon}</span>
        <div
          className={`absolute -bottom-0.5 -right-0.5 h-2.5 w-2.5 rounded-full border-2 border-background ${statusColors[connector.status]}`}
          title={statusLabels[connector.status]}
        />
      </div>

      {/* 信息 */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-xs font-medium text-foreground">
            {connector.name}
          </span>
          <span className="text-[10px] text-muted-foreground">
            {statusLabels[connector.status]}
          </span>
        </div>
        {connector.capabilities && connector.capabilities.length > 0 && (
          <div className="mt-0.5 flex flex-wrap gap-0.5">
            {connector.capabilities.slice(0, 3).map((cap) => (
              <span
                key={cap}
                className="rounded bg-muted px-1 py-0.5 text-[9px] text-muted-foreground"
              >
                {cap}
              </span>
            ))}
            {connector.capabilities.length > 3 && (
              <span className="text-[9px] text-muted-foreground">
                +{connector.capabilities.length - 3}
              </span>
            )}
          </div>
        )}
      </div>

      {/* 操作按钮 */}
      <div className="flex items-center gap-1">
        {connector.status === 'connected' && (
          <button
            className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
            onClick={() => onRefresh?.(connector.id)}
            title="刷新"
          >
            <RefreshCw className="h-3 w-3" />
          </button>
        )}

        <button
          className={`flex h-6 w-6 items-center justify-center rounded ${
            connector.status === 'connected'
              ? 'text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20'
              : 'text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20'
          } ${isOperating ? 'animate-pulse' : ''}`}
          onClick={handleToggle}
          disabled={isOperating}
          title={connector.status === 'connected' ? '断开' : '连接'}
        >
          {connector.status === 'connected' ? (
            <Unplug className="h-3 w-3" />
          ) : (
            <Plug className="h-3 w-3" />
          )}
        </button>

        <button
          className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground hover:bg-accent hover:text-foreground"
          onClick={() => onConfigure?.(connector.id)}
          title="配置"
        >
          <Settings className="h-3 w-3" />
        </button>
      </div>
    </div>
  )
}
