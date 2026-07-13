/**
 * WebSocket 连接状态指示器组件
 *
 * 显示 WebSocket 连接状态，提供手动重连功能
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

import { useWebSocket } from '../../hooks/useWebSocket'
import './WebSocketStatusIndicator.css'

/**
 * 状态配置
 */
const STATUS_CONFIG = {
  connected: {
    label: '已连接',
    color: 'success',
    icon: '●',
  },
  connecting: {
    label: '连接中',
    color: 'warning',
    icon: '○',
  },
  reconnecting: {
    label: '重连中',
    color: 'warning',
    icon: '○',
  },
  disconnected: {
    label: '未连接',
    color: 'default',
    icon: '○',
  },
  failed: {
    label: '连接失败',
    color: 'error',
    icon: '●',
  },
} as const

/**
 * WebSocketStatusIndicator 组件属性
 */
export interface WebSocketStatusIndicatorProps {
  /** 是否显示标签文本 */
  showLabel?: boolean
  /** 是否显示重连按钮 */
  showReconnectButton?: boolean
  /** 自定义类名 */
  className?: string
  /** 点击事件 */
  onClick?: () => void
}

/**
 * WebSocketStatusIndicator 组件
 *
 * @example
 * ```tsx
 * // 只显示状态点
 * <WebSocketStatusIndicator />
 *
 * // 显示状态点和标签
 * <WebSocketStatusIndicator showLabel />
 *
 * // 显示完整控制（状态、标签、重连按钮）
 * <WebSocketStatusIndicator showLabel showReconnectButton />
 * ```
 */
export function WebSocketStatusIndicator({
  showLabel = false,
  showReconnectButton = false,
  className = '',
  onClick,
}: WebSocketStatusIndicatorProps) {
  const { status, reconnect, reconnecting, failed } = useWebSocket()

  const config = STATUS_CONFIG[status]

  const canReconnect = failed || reconnecting

  const handleReconnect = () => {
    if (canReconnect) {
      reconnect()
    }
  }

  const containerClassName = [
    'ws-status-indicator',
    `ws-status-${config.color}`,
    canReconnect && showReconnectButton ? 'ws-status-clickable' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <div
      className={containerClassName}
      onClick={canReconnect && showReconnectButton ? handleReconnect : onClick}
      title={config.label}
    >
      {/* 状态指示点 */}
      <span className="ws-status-icon" aria-hidden="true">
        {config.icon}
      </span>

      {/* 状态标签 */}
      {showLabel && <span className="ws-status-label">{config.label}</span>}

      {/* 重连按钮 */}
      {showReconnectButton && canReconnect && (
        <button className="ws-reconnect-button" onClick={handleReconnect}>
          重连
        </button>
      )}
    </div>
  )
}
