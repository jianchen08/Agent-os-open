/**
 * SubAgent 卡片组件
 *
 * 显示子 Agent 的执行状态和摘要信息
 * 支持三种显示模式：collapsed / summary / full
 */

import { ChevronDown, ChevronRight, ExternalLink, MessageSquare } from 'lucide-react'
import { useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { AgentLevel } from '@/types/models'

/**
 * 显示模式
 */
export type SubAgentDisplayMode = 'collapsed' | 'summary' | 'full'

/**
 * SubAgent 状态
 */
export type SubAgentStatus = 'running' | 'waiting_input' | 'completed' | 'failed'

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
    <Badge variant={variant} className="h-4 px-1.5 py-0 text-xs">
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
          'inline-flex items-center gap-1.5 rounded-md px-2 py-1',
          'bg-muted/50 border-border/50 border',
          'text-muted-foreground text-xs',
          'hover:bg-muted hover:border-border',
          'transition-all duration-200',
          className,
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
        className={cn('bg-card rounded-lg border p-4', 'transition-all duration-200', className)}
      >
        <div className="mb-3 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {getLevelBadge(data.agentLevel)}
            <span className="font-medium">{data.name}</span>
            {data.path && data.path.length > 0 && (
              <span className="text-muted-foreground text-xs">{data.path.join(' \u2192 ')}</span>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <span className={cn('text-sm', getStatusColor(data.status))}>
              {getStatusIcon(data.status)}
            </span>
            <span className="text-muted-foreground text-xs capitalize">
              {data.status.replace('_', ' ')}
            </span>
          </div>
        </div>

        {data.summary && <div className="text-muted-foreground mb-3 text-sm">{data.summary}</div>}

        <div className="flex items-center justify-between">
          <div className="text-muted-foreground text-xs">
            {data.updatedAt && <span>更新于 {new Date(data.updatedAt).toLocaleTimeString()}</span>}
          </div>
          {onOpenDetail && (
            <Button variant="ghost" size="sm" onClick={handleOpenDetail} className="h-7 text-xs">
              <MessageSquare className="mr-1 h-3 w-3" />
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
        'bg-card rounded-lg border p-3',
        'hover:bg-accent/50 hover:border-border',
        'cursor-pointer transition-all duration-200',
        className,
      )}
      onClick={handleToggleExpand}
    >
      <div className="flex items-center justify-between">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {expandable && (
            <span className="text-muted-foreground flex-shrink-0">
              {isExpanded ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
            </span>
          )}
          {getLevelBadge(data.agentLevel)}
          <span className="truncate text-sm font-medium">{data.name}</span>
          <span className={cn('flex-shrink-0 text-xs', getStatusColor(data.status))}>
            {getStatusIcon(data.status)}
          </span>
        </div>

        {onOpenDetail && (
          <Button
            variant="ghost"
            size="sm"
            className="h-7 flex-shrink-0 px-2"
            onClick={(e) => {
              e.stopPropagation()
              handleOpenDetail()
            }}
          >
            <ExternalLink className="h-3 w-3" />
            <span className="ml-1 text-xs">详情</span>
          </Button>
        )}
      </div>

      {isExpanded && (
        <div className="border-border mt-2 border-t pt-2">
          {data.path && data.path.length > 0 && (
            <div className="text-muted-foreground mb-1.5 text-xs">
              路径: {data.path.join(' \u2192 ')}
            </div>
          )}
          {data.summary && <div className="text-muted-foreground text-sm">{data.summary}</div>}
          {data.updatedAt && (
            <div className="text-muted-foreground mt-1.5 text-xs">
              更新于 {new Date(data.updatedAt).toLocaleString()}
            </div>
          )}
          {data.taskId && (
            <div className="text-muted-foreground mt-1 text-xs">任务 ID: {data.taskId}</div>
          )}
        </div>
      )}
    </div>
  )
}
