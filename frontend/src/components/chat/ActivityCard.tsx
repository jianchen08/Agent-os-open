/**
 * ActivityCard 组件
 *
 * 统一的活动卡片组件，用于渲染工具调用、任务创建、任务阶段等所有活动
 */

import { useState } from 'react'
import {
  Ban,
  CheckCircle2,
  ChevronDown,
  Clock,
  ExternalLink,
  Loader2,
  Sparkles,
  Target,
  Wrench,
  XCircle,
} from '@/assets/icons'
import { ErrorSourceBadge } from '@/components/shared/ErrorSourceBadge'
import { TOOL_CONTENT_SCROLL_CLASS } from '@/lib/toolCardStyles'
import { cn } from '@/lib/utils'
import { DetailBlock } from './ActivityBlockViews'
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
      return <Clock className="h-icon-xs w-icon-xs" style={breatheStyle} />
    case 'running':
      return <Loader2 className="h-icon-xs w-icon-xs animate-spin" style={breatheStyle} />
    case 'completed':
      return <CheckCircle2 className="h-icon-xs w-icon-xs" style={breatheStyle} />
    case 'failed':
      return <XCircle className="h-icon-xs w-icon-xs" style={breatheStyle} />
    case 'cancelled':
      return <Ban className="h-icon-xs w-icon-xs" style={breatheStyle} />
    default:
      return <Clock className="h-icon-xs w-icon-xs" style={breatheStyle} />
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
      return <Wrench className="h-icon-md w-icon-md" />
    case 'task_created':
      return <Target className="h-icon-md w-icon-md" />
    case 'task_phase':
      return <Loader2 className="h-icon-md w-icon-md" />
    case 'task_completed':
      return <CheckCircle2 className="h-icon-md w-icon-md" />
    case 'task_failed':
      return <XCircle className="h-icon-md w-icon-md" />
    case 'agent_thinking':
      return <Sparkles className="h-icon-md w-icon-md" />
    default:
      return <Target className="h-icon-md w-icon-md" />
  }
}

/**
 * 复制按钮：code/json/log 块右上角，点击复制并短暂反馈
 */
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
  const { confirm, dialogState } = useConfirmDialog()

  // 注：不自动展开。业界惯例——即使工具失败也默认折叠，
  // 用户点击头部才展开查看错误详情（用户反馈：出错不应自动展开）。

  const handleHeaderClick = () => {
    setExpanded(!expanded)
    onHeaderClick?.()
  }

  const themeVars = getStatusThemeVars(activity.status, activity.customColor)

  /** 状态左边条样式：唯一贯穿整卡的状态指示（running 呼吸、cancelled 40% 透明） */
  const barStyle: CSSProperties = {
    backgroundColor: themeVars.color,
    ...(activity.status === 'running'
      ? { animation: 'breathe 2s ease-in-out infinite' }
      : {}),
    ...(activity.status === 'cancelled' ? { opacity: 0.4 } : {}),
  }

  return (
    <div
      className={cn(
        'border-border/40 bg-card relative my-1 overflow-hidden rounded-lg border text-xs transition-all',
        // 满宽：与消息气泡同宽（工具卡统一形态——不再提供紧凑自适应变体）
        'w-full max-w-full',
        activity.customClassName,
        className,
      )}
      style={style}
      data-activity-type={activity.type}
      data-activity-id={activity.id}
      data-activity-status={activity.status}
    >
      {/* 状态左边条 */}
      <span aria-hidden="true" className="absolute inset-y-0 left-0 w-[3px]" style={barStyle} />

      {/* 头部 */}
      <div
        className={cn(
          'flex cursor-pointer items-center gap-2 rounded-md py-1.5 pr-2.5 pl-3.5 transition-colors',
          'hover:bg-[var(--hover-overlay)]',
        )}
        onClick={handleHeaderClick}
      >
        {/* 类型图标（16px，主识别）：工具自定义图标或活动类型图标 */}
        <span className="text-muted-foreground flex-shrink-0">
          {getActivityTypeIcon(activity.type, activity.customIcon)}
        </span>

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

        {/* 条目摘要：文件路径/命令/URL 等一句话（折叠态可见，补足条目信息量） */}
        {activity.subtitle && (
          <span
            className="text-muted-foreground/70 min-w-0 max-w-[280px] truncate font-mono text-[11px]"
            title={activity.subtitle}
          >
            {activity.subtitle}
          </span>
        )}

        {activity.durationMs && (
          <span className="text-muted-foreground/70 flex-shrink-0">
            {formatDuration(activity.durationMs)}
          </span>
        )}

        {/* 增删行数徽标（如 file_write 的 +X -Y），颜色跟随主题 status 语义色 */}
        {activity.diffStat && (
          <span className="flex flex-shrink-0 items-center gap-2 font-mono text-xs font-semibold">
            <span className="text-status-success">+{activity.diffStat.added}</span>
            <span className="text-status-error">-{activity.diffStat.removed}</span>
          </span>
        )}

        {/* 状态图标（12px）：仅 running/failed 出现，其余状态由左边条表达，降低图标噪音 */}
        {(activity.status === 'running' || activity.status === 'failed') && (
          <span className="flex-shrink-0">{getStatusIcon(activity.status)}</span>
        )}

        {/* 条目上的打开文件入口：显式图标按钮（不依赖标题 hover 提示） */}
        {activity.filePath && activity.onOpenFile && (
          <button
            onClick={(e) => {
              e.stopPropagation()
              activity.onOpenFile?.(activity.filePath!)
            }}
            className="text-muted-foreground/60 hover:text-primary flex-shrink-0 rounded p-0.5 transition-colors hover:bg-[var(--hover-overlay)]"
            title={`在工作区打开文件: ${activity.filePath}`}
            aria-label={`打开文件 ${activity.filePath}`}
          >
            <ExternalLink className="h-icon-sm w-icon-sm" />
          </button>
        )}

        <span
          className={cn(
            'text-muted-foreground flex-shrink-0 transition-transform duration-200',
            expanded && 'rotate-180',
          )}
        >
          <ChevronDown className="h-icon-sm w-icon-sm" />
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
                    className={`bg-muted/30 ${TOOL_CONTENT_SCROLL_CLASS} rounded p-2 font-mono text-xs`}
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
              <div className="mb-1 flex items-center gap-1.5">
                <span className="text-xs font-medium text-status-error">错误</span>
                {typeof activity.error === 'object' && activity.error !== null && (
                  <ErrorSourceBadge source={(activity.error as { source?: string }).source} />
                )}
              </div>
              <pre className={`rounded bg-status-error/10 p-2 text-xs text-status-error ${TOOL_CONTENT_SCROLL_CLASS}`}>
                {typeof activity.error === 'object' && activity.error !== null
                  ? (activity.error as { message?: string }).message ?? JSON.stringify(activity.error)
                  : activity.error}
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
                    // 声明驱动产物可能无 handler（未知协议/value 缺失）——禁用而非报错
                    if (!action.onClick) return
                    if (action.confirmMessage && !(await confirm(action.confirmMessage))) {
                      return
                    }
                    await action.onClick()
                  }}
                  disabled={action.disabled || !action.onClick}
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
            className="fixed inset-0 bg-[var(--overlay-bg)]"
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
