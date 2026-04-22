/**
 * Agent Tab Item 组件
 *
 * 显示单个 Agent 标签页
 * 支持主 Tab（L1，不可关闭）和子 Tab（L2/L3，可关闭）
 */

import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { X } from 'lucide-react'
import type { AgentLevel } from '@/types/models'

/**
 * Agent Tab 状态
 */
export type AgentTabStatus =
  | 'running'
  | 'waiting_input'
  | 'completed'
  | 'failed'

/**
 * Agent Tab 数据接口
 */
export interface AgentTabItemData {
  /** Tab ID */
  id: string
  /** Agent 名称 */
  name: string
  /** Agent 层级 */
  agentLevel: AgentLevel | undefined
  /** Tab 状态 */
  status: AgentTabStatus
  /** 是否为当前激活 Tab */
  isActive: boolean
  /** 未读消息数 */
  unreadCount?: number
  /** 是否可关闭 */
  canClose: boolean
  /** Agent 路径 */
  path?: string[]
}

export interface AgentTabItemProps {
  /** Tab 数据 */
  tab: AgentTabItemData
  /** 点击回调 */
  onClick: () => void
  /** 关闭回调 */
  onClose?: () => void
  /** 自定义类名 */
  className?: string
}

/** 获取状态图标 */
const getStatusIcon = (status: AgentTabStatus) => {
  switch (status) {
    case 'running':
      return '\u25CF'
    case 'completed':
      return '\u2713'
    case 'waiting_input':
      return '\uD83D\uDCAC'
    case 'failed':
      return '\u2715'
    default:
      return '\u25CF'
  }
}

/** 获取 Agent 层级标签 */
const getAgentLevelBadge = (level: AgentLevel | undefined) => {
  if (!level) return null

  const levelConfig = {
    1: { label: 'L1', variant: 'default' as const },
    2: { label: 'L2', variant: 'secondary' as const },
    3: { label: 'L3', variant: 'outline' as const },
  }

  const config = levelConfig[level]

  return (
    <Badge variant={config.variant} className="text-xs px-1.5 py-0 h-4">
      {config.label}
    </Badge>
  )
}

/**
 * AgentTabItem 组件
 */
export const AgentTabItem: React.FC<AgentTabItemProps> = ({
  tab,
  onClick,
  onClose,
  className,
}) => {
  const isMainTab = tab.agentLevel === 1

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={e => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onClick()
        }
      }}
      className={cn(
        'group relative px-3 py-2 rounded-lg font-medium',
        'transition-all duration-200',
        'flex items-center gap-2 min-w-0 max-w-[200px]',
        isMainTab && 'bg-primary/5 border border-primary/20',
        tab.isActive
          ? 'bg-primary/15 text-primary border border-primary/30 shadow-sm'
          : 'hover:bg-accent text-muted-foreground hover:text-foreground border border-transparent',
        className
      )}
      title={tab.path?.join(' \u2192 ') || tab.name}
    >
      {getAgentLevelBadge(tab.agentLevel)}

      <span
        className={cn(
          'text-xs flex-shrink-0',
          tab.status === 'running' && 'text-primary animate-pulse',
          tab.status === 'waiting_input' && 'text-warning animate-pulse',
          tab.status === 'completed' && 'text-success',
          tab.status === 'failed' && 'text-destructive'
        )}
      >
        {getStatusIcon(tab.status)}
      </span>

      <span className="truncate text-sm font-medium">{tab.name}</span>

      {tab.unreadCount && tab.unreadCount > 0 && (
        <span className="px-1.5 py-0.5 rounded-full bg-warning text-warning-foreground text-xs font-medium flex-shrink-0">
          {tab.unreadCount > 9 ? '9+' : tab.unreadCount}
        </span>
      )}

      <span className="flex-shrink-0 w-4 h-4 flex items-center justify-center">
        {tab.canClose && onClose && (
          <button
            onClick={e => {
              e.stopPropagation()
              onClose()
            }}
            className={cn(
              'w-4 h-4 rounded flex items-center justify-center',
              'opacity-0 group-hover:opacity-100 transition-opacity',
              'hover:bg-destructive/20 text-muted-foreground hover:text-destructive'
            )}
            title="关闭 Tab"
          >
            <X className="w-3 h-3" />
          </button>
        )}
      </span>

      {tab.isActive && (
        <span className="absolute bottom-0 left-0 right-0 h-0.5 bg-primary rounded-full" />
      )}
    </div>
  )
}
