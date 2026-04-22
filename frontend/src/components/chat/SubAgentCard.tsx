/**
 * SubAgent 卡片组件
 *
 * 显示子 Agent 的执行状态和摘要信息
 * 支持三种显示模式：collapsed / summary / full
 */

import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  ChevronDown,
  ChevronRight,
  ExternalLink,
  MessageSquare,
} from 'lucide-react'
import { useState } from 'react'
import type { AgentLevel } from '@/types/models'

/**
 * 显示模式
 */
export type SubAgentDisplayMode = 'collapsed' | 'summary' | 'full'

/**
 * SubAgent 状态
 */
export type SubAgentStatus =
  | 'running'
  | 'waiting_input'
  | 'completed'
  | 'failed'

/**
 * SubAgent 数据
 */
export interface SubAgentData {
  /** Agent ID */
  id: string
  /** Agent 名称 */
  name: string
  /** Agent 层级 */
  agentLevel: AgentLevel
  /** Agent 状态 */
  status: SubAgentStatus
  /** 关联任务 ID */
  taskId?: string
  /** Agent 路径 */
  path?: string[]
  /** 执行摘要 */
  summary?: string
  /** 最后更新时间 */
  updatedAt?: string
}

export interface SubAgentCardProps {
  /** SubAgent 数据 */
  data: SubAgentData
  /** 显示模式 */
  mode?: SubAgentDisplayMode
  /** 是否可展开 */
  expandable?: boolean
  /** 展开按钮点击回调 */
  onExpand?: () => void
  /** 打开详情回调 */
  onOpenDetail?: () => void
  /** 自定义类名 */
  className?: string
}

/** 获取状态图标 */
const getStatusIcon = (status: SubAgentStatus) => {
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

/** 获取状态颜色 */
const getStatusColor = (status: SubAgentStatus) => {
  switch (status) {
    case 'running':
      return 'text-primary animate-pulse'
    case 'completed':
      return 'text-success'
    case 'waiting_input':
      return 'text-warning animate-pulse'
    case 'failed':
      return 'text-destructive'
    default:
      return 'text-muted-foreground'
  }
}

/** 获取层级标签 */
const getLevelBadge = (level: AgentLevel) => {
  const config = {
    1: { label: 'L1', variant: 'default' as const },
    2: { label: 'L2', variant: 'secondary' as const },
    3: { label: 'L3', variant: 'outline' as const },
  }

  const { label, variant } = config[level]

  return (
    <Badge variant={variant} className="text-xs px-1.5 py-0 h-4">
      {label}
    </Badge>
  )
}

/**
 * SubAgentCard 组件
 */
export const SubAgentCard: React.FC<SubAgentCardProps> = ({
  data,
  mode = 'summary',
  expandable = true,
  onExpand,
  onOpenDetail,
  className,
}) => {
  const [isExpanded, setIsExpanded] = useState(false)

  const handleToggleExpand = () => {
    if (!expandable) return
    setIsExpanded(!isExpanded)
    onExpand?.()
  }

  const handleOpenDetail = () => {
    onOpenDetail?.()
  }

  /** 收缩模式 */
  if (mode === 'collapsed') {
    return (
      <div
        className={cn(
          'inline-flex items-center gap-1.5 px-2 py-1 rounded-md',
          'bg-muted/50 border border-border/50',
          'text-xs text-muted-foreground',
          'hover:bg-muted hover:border-border',
          'transition-all duration-200',
          className
        )}
        title={`${data.name} - ${data.status}`}
      >
        {getLevelBadge(data.agentLevel)}
        <span className={cn('text-xs', getStatusColor(data.status))}>
          {getStatusIcon(data.status)}
        </span>
        <span className="max-w-[80px] truncate">{data.name}</span>
      </div>
    )
  }

  /** 完整模式 */
  if (mode === 'full') {
    return (
      <div
        className={cn(
          'p-4 rounded-lg border bg-card',
          'transition-all duration-200',
          className
        )}
      >
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            {getLevelBadge(data.agentLevel)}
            <span className="font-medium">{data.name}</span>
            {data.path && data.path.length > 0 && (
              <span className="text-xs text-muted-foreground">
                {data.path.join(' \u2192 ')}
              </span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <span className={cn('text-sm', getStatusColor(data.status))}>
              {getStatusIcon(data.status)}
            </span>
            <span className="text-xs text-muted-foreground capitalize">
              {data.status.replace('_', ' ')}
            </span>
          </div>
        </div>

        {data.summary && (
          <div className="text-sm text-muted-foreground mb-3">
            {data.summary}
          </div>
        )}

        <div className="flex items-center justify-between">
          <div className="text-xs text-muted-foreground">
            {data.updatedAt && (
              <span>
                更新于 {new Date(data.updatedAt).toLocaleTimeString()}
              </span>
            )}
          </div>
          {onOpenDetail && (
            <Button
              variant="ghost"
              size="sm"
              onClick={handleOpenDetail}
              className="h-7 text-xs"
            >
              <MessageSquare className="w-3 h-3 mr-1" />
              查看对话
            </Button>
          )}
        </div>
      </div>
    )
  }

  /** 缩略模式（summary） */
  return (
    <div
      className={cn(
        'p-3 rounded-lg border bg-card',
        'hover:bg-accent/50 hover:border-border',
        'transition-all duration-200 cursor-pointer',
        className
      )}
      onClick={handleToggleExpand}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2 flex-1 min-w-0">
          {expandable && (
            <span className="flex-shrink-0 text-muted-foreground">
              {isExpanded ? (
                <ChevronDown className="w-4 h-4" />
              ) : (
                <ChevronRight className="w-4 h-4" />
              )}
            </span>
          )}
          {getLevelBadge(data.agentLevel)}
          <span className="font-medium text-sm truncate">{data.name}</span>
          <span
            className={cn('text-xs flex-shrink-0', getStatusColor(data.status))}
          >
            {getStatusIcon(data.status)}
          </span>
        </div>

        {onOpenDetail && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 px-2 flex-shrink-0"
            onClick={e => {
              e.stopPropagation()
              handleOpenDetail()
            }}
          >
            <ExternalLink className="w-3 h-3" />
            <span className="ml-1 text-xs">详情</span>
          </Button>
        )}
      </div>

      {isExpanded && (
        <div className="mt-2 pt-2 border-t border-border">
          {data.path && data.path.length > 0 && (
            <div className="text-xs text-muted-foreground mb-1.5">
              路径: {data.path.join(' \u2192 ')}
            </div>
          )}
          {data.summary && (
            <div className="text-sm text-muted-foreground">{data.summary}</div>
          )}
          {data.updatedAt && (
            <div className="text-xs text-muted-foreground mt-1.5">
              更新于 {new Date(data.updatedAt).toLocaleString()}
            </div>
          )}
          {data.taskId && (
            <div className="text-xs text-muted-foreground mt-1">
              任务 ID: {data.taskId}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
