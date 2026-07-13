/**
 * ActivityCard 组件
 *
 * 统一的活动卡片组件，用于渲染工具调用、任务创建、任务阶段等所有活动
 */

import {
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  FileText,
  Loader2,
  Sparkles,
  Target,
  Wrench,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import { TextDiffView } from '@/components/approval'
import { MarkdownRenderer } from '@/components/chat/markdown/MarkdownRenderer'
import { cn } from '@/lib/utils'
import { formatDuration } from '@/types/activity'
import { useConfirmDialog } from '@/utils/confirm'
import type {
  ActivityAction,
  ActivityCardProps,
  ActivityData,
  ActivityDetailBlock,
  ActivityStatus,
  ActivityType,
} from '@/types/activity'
import type { CSSProperties, FC, ReactNode } from 'react'

/**
 * 获取状态对应的主题 CSS 变量色值
 * @param status 活动状态
 * @param customRunningColor 自定义运行颜色（用于阻塞型工具如 human_interaction）
 */
function getStatusThemeVars(
  status: ActivityStatus,
  customRunningColor?: string,
): {
  color: string
  border: string
  bg: string
  shadow: string
} {
  const varsMap: Record<
    ActivityStatus,
    { color: string; border: string; bg: string; shadow: string }
  > = {
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

  const result = varsMap[status] || varsMap.pending

  if (status === 'running' && customRunningColor) {
    return {
      color: customRunningColor,
      border: customRunningColor,
      bg: `color-mix(in srgb, ${customRunningColor} 8%, transparent)`,
      shadow: `0 0 8px color-mix(in srgb, ${customRunningColor} 15%, transparent)`,
    }
  }

  return result
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
      return <Clock className="h-3 w-3" style={breatheStyle} />
    case 'running':
      return <Loader2 className="h-3 w-3 animate-spin" style={breatheStyle} />
    case 'completed':
      return <CheckCircle2 className="h-3 w-3" style={breatheStyle} />
    case 'failed':
      return <XCircle className="h-3 w-3" style={breatheStyle} />
    case 'cancelled':
      return <Ban className="h-3 w-3" style={breatheStyle} />
    default:
      return <Clock className="h-3 w-3" style={breatheStyle} />
  }
}

/**
 * 获取活动类型图标
 */
function getActivityTypeIcon(type: ActivityType, customIcon?: ReactNode): ReactNode {
  if (customIcon) {
    return customIcon
  }

  switch (type) {
    case 'tool_call':
      return <Wrench className="h-4 w-4" />
    case 'task_created':
      return <Target className="h-4 w-4" />
    case 'task_phase':
      return <Loader2 className="h-4 w-4" />
    case 'task_completed':
      return <CheckCircle2 className="h-4 w-4" />
    case 'task_failed':
      return <XCircle className="h-4 w-4" />
    case 'agent_thinking':
      return <Sparkles className="h-4 w-4" />
    default:
      return <Target className="h-4 w-4" />
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
        <pre className="bg-muted/30 overflow-x-auto rounded p-2 font-mono text-xs">
          {JSON.stringify(content, null, 2)}
        </pre>
      )
    }

    switch (contentType) {
      case 'json':
        try {
          const parsed = JSON.parse(content)
          return (
            <pre className="bg-muted/30 overflow-x-auto rounded p-2 font-mono text-xs">
              {JSON.stringify(parsed, null, 2)}
            </pre>
          )
        } catch {
          return (
            <pre className="bg-muted/30 overflow-x-auto rounded p-2 font-mono text-xs whitespace-pre-wrap">
              {content}
            </pre>
          )
        }

      case 'code':
        return (
          <pre
            className={cn(
              'bg-muted/30 overflow-x-auto rounded p-2 font-mono text-xs',
              block.language && `language-${block.language}`,
            )}
          >
            <code>{content}</code>
          </pre>
        )

      case 'diff':
        return (
          <div className="bg-muted/30 overflow-x-auto rounded">
            <TextDiffView
              oldContent={block.diffOld ?? ''}
              newContent={block.diffNew ?? ''}
            />
          </div>
        )

      case 'markdown':
        return (
          <div className="bg-muted/30 max-w-none rounded p-2 text-xs">
            <MarkdownRenderer content={content} />
          </div>
        )

      case 'text':
      default:
        return (
          <pre className="bg-muted/30 overflow-x-auto rounded p-2 text-xs whitespace-pre-wrap">
            {content}
          </pre>
        )
    }
  }

  if (!block.collapsible) {
    return (
      <div className="space-y-1.5">
        <div className="text-muted-foreground text-xs font-medium">{block.label}</div>
        {renderContent()}
      </div>
    )
  }

  return (
    <div className="space-y-1.5">
      <button
        onClick={() => setExpanded(!expanded)}
        className="text-muted-foreground hover:text-foreground flex items-center gap-1.5 text-xs font-medium transition-colors"
      >
        {expanded ? (
          <ChevronDown className="h-3.5 w-3.5" />
        ) : (
          <ChevronRight className="h-3.5 w-3.5" />
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

  const themeVars = getStatusThemeVars(activity.status, activity.customColor)

  /** 卡片容器样式 */
  const cardStyle: CSSProperties = {
    borderColor: themeVars.border,
    backgroundColor: themeVars.bg,
    ...(activity.status === 'running'
      ? {
          animation: 'card-breathe 2s ease-in-out infinite',
          ...(activity.customColor
            ? { '--card-breathe-color': activity.customColor } as CSSProperties
            : {}),
        }
      : {}),
    ...style,
  }

  return (
    <div
      className={cn(
        'my-1 overflow-hidden rounded-lg text-xs transition-all',
        'w-fit max-w-[85%] border',
        activity.customClassName,
        className,
      )}
      style={cardStyle}
      data-activity-type={activity.type}
      data-activity-id={activity.id}
      data-activity-status={activity.status}
    >
      {/* 头部 */}
      <div
        className={cn(
          'flex cursor-pointer items-center gap-1.5 rounded-md px-2.5 py-1.5 transition-colors',
          'hover:bg-black/[0.03] dark:hover:bg-white/[0.04]',
        )}
        onClick={handleHeaderClick}
      >
        <span className="flex-shrink-0">{getStatusIcon(activity.status)}</span>

        {/* 文件名（可点击打开）或标题 */}
        {activity.filePath && activity.onOpenFile ? (
          <span
            className="text-primary min-w-0 cursor-pointer truncate font-medium hover:underline"
            onClick={(e) => {
              e.stopPropagation()
              activity.onOpenFile?.(activity.filePath!)
            }}
            title={`点击打开文件: ${activity.filePath}`}
          >
            {activity.title}
          </span>
        ) : (
          <span className="text-foreground min-w-0 truncate font-medium">{activity.title}</span>
        )}

        {activity.durationMs && (
          <span className="text-muted-foreground/70 flex-shrink-0">
            {formatDuration(activity.durationMs)}
          </span>
        )}

        {/* 增删行数徽标（如 file_write 的 +X -Y），颜色跟随主题 status 语义色 */}
        {activity.diffStat && (
          <span className="ml-2 flex flex-shrink-0 items-center gap-2 font-mono text-xs font-semibold">
            <span className="text-status-success">+{activity.diffStat.added}</span>
            <span className="text-status-error">-{activity.diffStat.removed}</span>
          </span>
        )}

        <span
          className={cn(
            'text-muted-foreground flex-shrink-0 transition-transform duration-200',
            expanded && 'rotate-180',
          )}
        >
          <ChevronDown className="h-3 w-3" />
        </span>
      </div>

      {/* 进度条 */}
      {activity.progress !== undefined && activity.progress > 0 && (
        <div className="px-2.5 pb-1.5">
          <div className="bg-muted/50 h-1 w-full overflow-hidden rounded-full">
            <div
              className="h-full transition-all duration-300 ease-out"
              style={{
                backgroundColor: themeVars.color,
                width: `${Math.min(100, Math.max(0, activity.progress))}%`,
              }}
            />
          </div>
          <div className="mt-0.5 flex items-center justify-between">
            <div className="text-muted-foreground text-xs">
              {activity.currentStep && (
                <span className="inline-block max-w-[200px] truncate">{activity.currentStep}</span>
              )}
            </div>
            <div className="text-muted-foreground text-right text-xs">
              {activity.progress}%
              {activity.estimatedRemainingMs && (
                <span className="ml-2">剩余 {formatDuration(activity.estimatedRemainingMs)}</span>
              )}
            </div>
          </div>
        </div>
      )}

      {/* 展开的详情区域 */}
      {expanded && (
        <div className="bg-muted/5 mx-1 mb-1 space-y-1.5 rounded-md px-2 py-1.5">
          {activity.partialOutput && activity.partialOutput.length > 0 && (
            <div>
              <div className="text-muted-foreground mb-1 text-xs font-medium">实时输出</div>
              <div className="space-y-1">
                {activity.partialOutput.map((output, index) => (
                  <pre
                    key={`partial-${index}`}
                    className="bg-muted/30 rounded p-2 font-mono text-xs whitespace-pre-wrap"
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
              <div className="mb-1 text-xs font-medium text-status-error">错误</div>
              <pre className="rounded bg-status-error/10 p-2 text-xs whitespace-pre-wrap text-status-error">
                {activity.error}
              </pre>
            </div>
          )}

          {activity.actions && activity.actions.length > 0 && (
            <div className="border-border/20 flex items-center gap-2 border-t pt-1.5">
              {activity.actions.map((action) => (
                <button
                  key={action.id}
                  onClick={async (e) => {
                    e.stopPropagation()
                    if (action.confirmMessage && !(await confirm(action.confirmMessage))) {
                      return
                    }
                    await action.onClick()
                  }}
                  disabled={action.disabled}
                  aria-label={action.label || action.type}
                  className={cn(
                    'inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors',
                    'disabled:cursor-not-allowed disabled:opacity-50',
                    action.variant === 'destructive' &&
                      'bg-status-error/15 text-status-error hover:bg-status-error/20',
                    action.variant === 'ghost' &&
                      'hover:bg-muted/70 text-muted-foreground hover:text-foreground',
                    action.variant === 'outline' &&
                      'border-border hover:bg-muted/70 text-muted-foreground border',
                    (!action.variant || action.variant === 'default') &&
                      'bg-primary/10 text-primary hover:bg-primary/20',
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
          <div className="bg-background border-border relative z-10 mx-4 w-full max-w-sm rounded-lg border p-4 shadow-lg">
            <p className="text-foreground mb-4 text-sm">{dialogState.message}</p>
            <div className="flex justify-end gap-2">
              <button
                onClick={() => dialogState.onCancel()}
                className="border-border hover:bg-muted/70 text-muted-foreground rounded-md border px-3 py-1.5 text-xs transition-colors"
              >
                取消
              </button>
              <button
                onClick={() => dialogState.onConfirm()}
                className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-3 py-1.5 text-xs transition-colors"
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

export type { ActivityAction, ActivityCardProps, ActivityData, ActivityDetailBlock }
export default ActivityCard
