/**
 * ActivityCard 组件
 *
 * 统一的活动卡片组件，用于渲染工具调用、任务创建、任务阶段等所有活动
 */

import { cn } from '@/lib/utils'
import type {
  ActivityAction,
  ActivityCardProps,
  ActivityData,
  ActivityDetailBlock,
  ActivityStatus,
  ActivityType,
} from '@/types/activity'
import {
  formatDuration,
} from '@/types/activity'
import {
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
  Sparkles,
  Target,
  Wrench,
  XCircle,
} from 'lucide-react'
import type { CSSProperties, FC, ReactNode } from 'react'
import { useState } from 'react'
import { useConfirmDialog } from '@/utils/confirm'

/**
 * 获取状态对应的主题 CSS 变量色值
 */
function getStatusThemeVars(status: ActivityStatus): {
  color: string
  border: string
  bg: string
  shadow: string
} {
  const varsMap: Record<ActivityStatus, { color: string; border: string; bg: string; shadow: string }> = {
    pending: {
      color: 'var(--accent-waiting, #f59e0b)',
      border: 'var(--accent-waiting, #f59e0b)',
      bg: 'color-mix(in srgb, var(--accent-waiting, #f59e0b) 8%, transparent)',
      shadow: '0 0 8px color-mix(in srgb, var(--accent-waiting, #f59e0b) 15%, transparent)',
    },
    running: {
      color: 'var(--accent-running, #00f0ff)',
      border: 'var(--accent-running, #00f0ff)',
      bg: 'color-mix(in srgb, var(--accent-running, #00f0ff) 8%, transparent)',
      shadow: '0 0 8px color-mix(in srgb, var(--accent-running, #00f0ff) 15%, transparent)',
    },
    completed: {
      color: 'var(--accent-success, #10b981)',
      border: 'var(--accent-success, #10b981)',
      bg: 'color-mix(in srgb, var(--accent-success, #10b981) 8%, transparent)',
      shadow: '0 0 8px color-mix(in srgb, var(--accent-success, #10b981) 15%, transparent)',
    },
    failed: {
      color: 'var(--accent-error, #ef4444)',
      border: 'var(--accent-error, #ef4444)',
      bg: 'color-mix(in srgb, var(--accent-error, #ef4444) 8%, transparent)',
      shadow: '0 0 8px color-mix(in srgb, var(--accent-error, #ef4444) 15%, transparent)',
    },
    cancelled: {
      color: 'var(--accent-pending, #94a3b8)',
      border: 'var(--accent-pending, #94a3b8)',
      bg: 'color-mix(in srgb, var(--accent-pending, #94a3b8) 6%, transparent)',
      shadow: 'none',
    },
  }
  return varsMap[status] || varsMap.pending
}

/**
 * 获取状态图标
 */
function getStatusIcon(status: ActivityStatus): ReactNode {
  const themeVars = getStatusThemeVars(status)
  const breatheStyle: React.CSSProperties =
    status === 'running'
      ? { animation: 'breathe 2s ease-in-out infinite', color: themeVars.color }
      : { color: themeVars.color }

  switch (status) {
    case 'pending':
      return <Clock className="w-3 h-3" style={breatheStyle} />
    case 'running':
      return <Loader2 className="w-3 h-3 animate-spin" style={breatheStyle} />
    case 'completed':
      return <CheckCircle2 className="w-3 h-3" style={breatheStyle} />
    case 'failed':
      return <XCircle className="w-3 h-3" style={breatheStyle} />
    case 'cancelled':
      return <Ban className="w-3 h-3" style={breatheStyle} />
    default:
      return <Clock className="w-3 h-3" style={breatheStyle} />
  }
}

/**
 * 获取活动类型图标
 */
function getActivityTypeIcon(
  type: ActivityType,
  customIcon?: ReactNode
): ReactNode {
  if (customIcon) {
    return customIcon
  }

  switch (type) {
    case 'tool_call':
      return <Wrench className="w-4 h-4" />
    case 'task_created':
      return <Target className="w-4 h-4" />
    case 'task_phase':
      return <Loader2 className="w-4 h-4" />
    case 'task_completed':
      return <CheckCircle2 className="w-4 h-4" />
    case 'task_failed':
      return <XCircle className="w-4 h-4" />
    case 'agent_thinking':
      return <Sparkles className="w-4 h-4" />
    default:
      return <Target className="w-4 h-4" />
  }
}

/**
 * 详情区块组件
 */
const DetailBlock: FC<{ block: ActivityDetailBlock }> = ({ block }) => {
  const [expanded, setExpanded] = useState(block.defaultExpanded ?? true)

  /** 渲染内容 */
  const renderContent = () => {
    const content = block.content
    const contentType = block.contentType || 'text'

    if (typeof content === 'object') {
      return (
        <pre className="text-xs bg-muted/30 p-2 rounded overflow-x-auto font-mono">
          {JSON.stringify(content, null, 2)}
        </pre>
      )
    }

    switch (contentType) {
      case 'json':
        try {
          const parsed = JSON.parse(content)
          return (
            <pre className="text-xs bg-muted/30 p-2 rounded overflow-x-auto font-mono">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          )
        } catch {
          return (
            <pre className="text-xs bg-muted/30 p-2 rounded overflow-x-auto whitespace-pre-wrap font-mono">
              {content}
            </pre>
          )
        }

      case 'code':
        return (
          <pre
            className={cn(
              'text-xs bg-muted/30 p-2 rounded overflow-x-auto font-mono',
              block.language && `language-${block.language}`
            )}
          >
            <code>{content}</code>
          </pre>
        )

      case 'markdown':
        return (
          <div className="text-xs bg-muted/30 p-2 rounded prose prose-sm dark:prose-invert max-w-none">
            {content}
          </div>
        )

      case 'text':
      default:
        return (
          <pre className="text-xs bg-muted/30 p-2 rounded overflow-x-auto whitespace-pre-wrap">
            {content}
          </pre>
        )
    }
  }

  if (!block.collapsible) {
    return (
      <div className="space-y-1.5">
        <div className="text-xs font-medium text-muted-foreground">
          {block.label}
        </div>
        {renderContent()}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground hover:text-foreground transition-colors"
      >
        {expanded ? (
          <ChevronDown className="w-3.5 h-3.5" />
        ) : (
          <ChevronRight className="w-3.5 h-3.5" />
        )}
        {block.label}
      </button>
      {expanded && renderContent()}
    </div>
  )
}

/**
 * ActivityCard 主组件
 */
const ActivityCard: FC<ActivityCardProps> = ({
  activity,
  defaultExpanded = false,
  onHeaderClick,
  className,
  style,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const { confirm, dialogState, setDialogState } = useConfirmDialog()

  const handleHeaderClick = () => {
    setExpanded(!expanded)
    onHeaderClick?.()
  }

  const themeVars = getStatusThemeVars(activity.status)

  /** 卡片容器样式 */
  const cardStyle: CSSProperties = {
    borderColor: themeVars.border,
    backgroundColor: themeVars.bg,
    ...(activity.status === 'running'
      ? { animation: 'card-breathe 2s ease-in-out infinite' }
      : {}),
    ...style,
  }

  return (
    <div
      className={cn(
        'my-1 rounded-lg text-xs overflow-hidden transition-all',
        'border w-fit max-w-[85%]',
        activity.customClassName,
        className
      )}
      style={cardStyle}
      data-activity-type={activity.type}
      data-activity-id={activity.id}
      data-activity-status={activity.status}
    >
      {/* 头部 */}
      <div
        className={cn(
          'flex items-center gap-1.5 px-2.5 py-1.5 cursor-pointer transition-colors rounded-md',
          'hover:bg-black/[0.03] dark:hover:bg-white/[0.04]',
        )}
        onClick={handleHeaderClick}
      >
        <span className="flex-shrink-0">
          {getStatusIcon(activity.status)}
        </span>

        <span className="font-medium text-foreground truncate min-w-0">
          {activity.title}
        </span>

        {activity.durationMs && (
          <span className="text-muted-foreground/70 flex-shrink-0">
            {formatDuration(activity.durationMs)}
          </span>
        )}

        <span
          className={cn(
            'transition-transform duration-200 flex-shrink-0 text-muted-foreground',
            expanded && 'rotate-180'
          )}
        >
          <ChevronDown className="w-3 h-3" />
        </span>
      </div>

      {/* 进度条 */}
      {activity.progress !== undefined && activity.progress > 0 && (
        <div className="px-2.5 pb-1.5">
          <div className="w-full bg-muted/50 rounded-full h-1 overflow-hidden">
            <div
              className="h-full transition-all duration-300 ease-out"
              style={{
                backgroundColor: themeVars.color,
                width: `${Math.min(100, Math.max(0, activity.progress))}%`,
              }}
            />
          </div>
          <div className="flex items-center justify-between mt-0.5">
            <div className="text-xs text-muted-foreground">
              {activity.currentStep && (
                <span className="truncate max-w-[200px] inline-block">
                  {activity.currentStep}
                </span>
              )}
            </div>
            <div className="text-xs text-muted-foreground text-right">
              {activity.progress}%
              {activity.estimatedRemainingMs && (
                <span className="ml-2">
                  剩余 {formatDuration(activity.estimatedRemainingMs)}
                </span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 展开的详情区域 */}
      {expanded && (
        <div className="px-2 py-1.5 space-y-1.5 bg-muted/5 rounded-md mx-1 mb-1">
          {activity.partialOutput && activity.partialOutput.length > 0 && (
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1">
                实时输出
              </div>
              <div className="space-y-1">
                {activity.partialOutput.map((output, index) => (
                  <pre
                    key={`partial-${index}`}
                    className="text-xs bg-muted/30 p-2 rounded whitespace-pre-wrap font-mono"
                  >
                    {output}
                  </pre>
                ))}
              </div>
            </div>
          )}

          {activity.details?.map((detail, index) => (
            <DetailBlock key={detail.id || `detail-${index}`} block={detail} />
          ))}

          {activity.error && (
            <div>
              <div className="text-xs font-medium text-red-500 dark:text-red-400 mb-1">
                错误
              </div>
              <pre className="text-xs bg-red-50/50 dark:bg-red-900/10 text-red-600 dark:text-red-400 p-2 rounded whitespace-pre-wrap">
                {activity.error}
              </pre>
            </div>
          )}

          {activity.actions && activity.actions.length > 0 && (
            <div className="flex items-center gap-2 pt-1.5 border-t border-border/20">
              {activity.actions.map(action => (
                <button
                  key={action.id}
                  onClick={async e => {
                    e.stopPropagation()
                    if (
                      action.confirmMessage &&
                      !(await confirm(action.confirmMessage))
                    ) {
                      return
                    }
                    await action.onClick()
                  }}
                  disabled={action.disabled}
                  aria-label={action.label || action.type}
                  className={cn(
                    'inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-xs font-medium transition-colors',
                    'disabled:opacity-50 disabled:cursor-not-allowed',
                    action.variant === 'destructive' &&
                      'bg-red-100 text-red-700 hover:bg-red-200 dark:bg-red-900/30 dark:text-red-400 dark:hover:bg-red-900/50',
                    action.variant === 'ghost' &&
                      'hover:bg-muted/70 text-muted-foreground hover:text-foreground',
                    action.variant === 'outline' &&
                      'border border-border hover:bg-muted/70 text-muted-foreground',
                    (!action.variant || action.variant === 'default') &&
                      'bg-primary/10 text-primary hover:bg-primary/20'
                  )}
                  title={action.label}
                >
                  {action.icon}
                  <span>{action.label}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 确认弹窗 */}
      {dialogState.open && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center"
          role="dialog"
          aria-modal="true"
          aria-label="确认操作"
        >
          <div
            className="fixed inset-0 bg-black/50"
            onClick={() => {
              dialogState.onCancel()
            }}
          />
          <div className="relative bg-background border border-border rounded-lg shadow-lg p-4 max-w-sm w-full mx-4 z-10">
            <p className="text-sm text-foreground mb-4">{dialogState.message}</p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => dialogState.onCancel()}
                className="px-3 py-1.5 text-xs rounded-md border border-border hover:bg-muted/70 text-muted-foreground transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => dialogState.onConfirm()}
                className="px-3 py-1.5 text-xs rounded-md bg-primary text-primary-foreground hover:bg-primary/90 transition-colors"
              >
                确认
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export type {
  ActivityAction,
  ActivityCardProps,
  ActivityData,
  ActivityDetailBlock,
}
export default ActivityCard
