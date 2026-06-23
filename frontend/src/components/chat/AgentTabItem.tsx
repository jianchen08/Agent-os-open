/**
 * Agent Tab Item 组件
 *
 * 显示单个 Agent 标签页
 * 支持主 Tab（L1，不可关闭）和子 Tab（L2/L3，可关闭）
 *
 * 注意：Agent 切换功能位于会话列表三点菜单（SessionList），
 *       模型名显示位于对话栏 header（ChatContainer），
 *       标签栏仅负责 Tab 导航。
 */

import { X } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import type { AgentLevel } from '@/types/models'

/**
 * Agent Tab 状态
 */
export type AgentTabStatus = 'running' | 'waiting_input' | 'completed' | 'failed'

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
    <Badge variant={config.variant} className="h-4 px-1.5 py-0 text-xs">
      {config.label}
    </Badge>
  )
}

/**
 * AgentTabItem 组件
 */
export const AgentTabItem: React.FC<AgentTabItemProps> = ({ tab, onClick, onClose, className }) => {
  const isMainTab = tab.agentLevel === 1

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onClick()
        }
      }}
      className={cn(
        'group relative rounded-lg px-3 py-2 font-medium',
        'transition-all duration-200',
        'flex max-w-[200px] min-w-0 items-center gap-2',
        isMainTab && 'bg-primary/5 border-primary/20 border',
        tab.isActive
          ? 'bg-primary/15 text-primary border-primary/30 border shadow-sm'
          : 'hover:bg-accent text-muted-foreground hover:text-foreground border border-transparent',
        className,
      )}
      title={tab.path?.join(' \u2192 ') || tab.name}
    >
      <span
        className={cn(
          'flex-shrink-0 text-xs',
          tab.status === 'running' && 'text-primary animate-pulse',
          tab.status === 'waiting_input' && 'text-warning animate-pulse',
          tab.status === 'completed' && 'text-success',
          tab.status === 'failed' && 'text-destructive',
        )}
      >
        {getStatusIcon(tab.status)}
      </span>

      {getAgentLevelBadge(tab.agentLevel)}

      <span className="truncate text-sm font-medium">{tab.name}</span>

      {tab.unreadCount && tab.unreadCount > 0 && (
        <span className="bg-warning text-warning-foreground flex-shrink-0 rounded-full px-1.5 py-0.5 text-xs font-medium">
          {tab.unreadCount > 9 ? '9+' : tab.unreadCount}
        </span>
      )}

      <span className="flex h-4 w-4 flex-shrink-0 items-center justify-center">
        {tab.canClose && onClose && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              onClose()
            }}
            className={cn(
              'flex h-4 w-4 items-center justify-center rounded',
              'opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity',
              'hover:bg-destructive/20 text-muted-foreground hover:text-destructive',
            )}
            title="关闭 Tab"
          >
            <X className="h-3 w-3" />
          </button>
        )}
      </span>

      {tab.isActive && (
        <span className="bg-primary absolute right-0 bottom-0 left-0 h-0.5 rounded-full" />
      )}
    </div>
  )
}
