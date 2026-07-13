/**
 * NotificationItem - 单条通知组件
 *
 * 功能：
 * - 优先级视觉区分（图标、颜色、动画）
 * - 进度条（progress类型）
 * - 忽略/确认按钮
 * - 阻塞模式覆盖层
 * - 折叠后的摘要行
 */

import {
  AlertCircle,
  AlertTriangle,
  Bell,
  CheckCircle2,
  Info,
  Loader2,
  X,
} from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { PRIORITY_STYLES } from '@/types/notification'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import type {
  NotificationAction,
  NotificationCategory,
  NotificationItem as NotificationItemType,
  NotificationPriority,
} from '@/types/notification'

/** 图标映射 */
const CATEGORY_ICONS: Record<NotificationCategory, React.ElementType> = {
  progress: Loader2,
  alert: AlertTriangle,
  info: Info,
  success: CheckCircle2,
  error: AlertCircle,
}

/** 优先级图标覆盖 */
const PRIORITY_ICONS: Partial<Record<NotificationPriority, React.ElementType>> = {
  critical: AlertTriangle,
  high: AlertCircle,
}

export interface NotificationItemProps {
  /** 通知数据 */
  notification: NotificationItemType
  /** 是否折叠 */
  isCollapsed?: boolean
  /** 点击通知回调 */
  onClick?: (notification: NotificationItemType) => void
  /** 忽略回调 */
  onDismiss?: (id: string) => void
  /** 执行动作回调 */
  onAction?: (notificationId: string, action: NotificationAction) => void
  /** 自定义类名 */
  className?: string
  /** 是否关联人类交互（由 NotificationCenter 传入） */
  hasInteraction?: boolean
}

/** 格式化时间 */
function formatTime(timestamp: string): string {
  const date = new Date(timestamp)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSec = Math.floor(diffMs / 1000)

  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
  return date.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export function NotificationItemComponent({
  notification,
  isCollapsed = false,
  onClick,
  onDismiss,
  onAction,
  className,
  hasInteraction = false,
}: NotificationItemProps) {
  const { id, title, message, priority, category, progress, isBlocking, isRead, timestamp, actions } = notification

  const style = PRIORITY_STYLES[priority]
  const IconComponent = PRIORITY_ICONS[priority] ?? CATEGORY_ICONS[category]
  const isProgress = category === 'progress'

  const handleClick = useCallback(() => {
    onClick?.(notification)
  }, [onClick, notification])

  const handleDismiss = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      onDismiss?.(id)
    },
    [onDismiss, id],
  )

  const handleAction = useCallback(
    (e: React.MouseEvent, action: NotificationAction) => {
      e.stopPropagation()
      onAction?.(id, action)
    },
    [onAction, id],
  )

  /** 折叠模式：只显示一行摘要 */
  if (isCollapsed) {
    return (
      <div
        className={cn(
          'flex items-center gap-2 px-3 py-1.5 text-sm rounded-lg',
          hasInteraction ? 'cursor-pointer hover:bg-accent/60' : 'cursor-default',
          'hover:bg-muted/50 transition-colors',
          !isRead && 'font-medium',
          className,
        )}
        onClick={handleClick}
        data-testid={`notification-item-${id}`}
      >
        <IconComponent className={cn('h-3.5 w-3.5 flex-shrink-0', style.text)} />
        <span className="truncate flex-1">{title}</span>
        {hasInteraction && (
          <span className="text-[10px] text-primary font-medium shrink-0">点击处理</span>
        )}
        <span className="text-xs text-muted-foreground flex-shrink-0">{formatTime(timestamp)}</span>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'relative rounded-xl border transition-all duration-200',
        style.bg,
        style.border,
        isBlocking && 'ring-2 ring-ring/50 shadow-lg',
        !isRead && 'border-l-4',
        style.pulse && 'animate-pulse-subtle',
        isRead && 'opacity-70',
        hasInteraction && 'cursor-pointer hover:shadow-md',
        className,
      )}
      onClick={handleClick}
      data-testid={`notification-item-${id}`}
      role="alert"
    >
      {/* 阻塞模式遮罩提示 */}
      {isBlocking && (
        <div className="absolute -top-6 left-0 right-0 flex items-center justify-center">
          <span className="bg-red-500 text-white text-xs px-3 py-0.5 rounded-full font-medium">
            ⚠️ 需要确认
          </span>
        </div>
      )}

      <div className="p-3">
        {/* 标题行 */}
        <div className="flex items-start gap-2">
          <IconComponent
            className={cn(
              'h-4 w-4 flex-shrink-0 mt-0.5',
              style.text,
              isProgress && 'animate-spin',
            )}
          />
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2">
              <span className={cn('text-sm font-semibold', !isRead && style.text)}>
                {title}
              </span>
              {!isRead && (
                <span className="h-2 w-2 rounded-full bg-primary flex-shrink-0" />
              )}
            </div>
            <span className="text-xs text-muted-foreground">{formatTime(timestamp)}</span>
          </div>

          {/* 忽略按钮 */}
          {!isBlocking && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 w-6 p-0 rounded-full opacity-100 md:opacity-0 md:group-hover:opacity-100 hover:opacity-100 transition-opacity"
              onClick={handleDismiss}
              aria-label="忽略通知"
            >
              <X className="h-3.5 w-3.5" />
            </Button>
          )}
        </div>

        {/* 消息内容 */}
        {message && (
          <div className="mt-2 text-sm text-muted-foreground pl-6 max-h-[300px] overflow-y-auto overscroll-contain rounded">
            <MarkdownRenderer content={message} />
          </div>
        )}

        {/* 进度条 */}
        {isProgress && progress != null && (
          <div className="mt-2 pl-6">
            <div className="flex items-center gap-2">
              <div className="flex-1 h-2 rounded-full bg-muted overflow-hidden">
                <div
                  className={cn(
                    'h-full rounded-full transition-all duration-300',
                    progress >= 100 ? 'bg-green-500' : 'bg-primary',
                  )}
                  style={{ width: `${Math.min(100, Math.max(0, progress))}%` }}
                />
              </div>
              <span className="text-xs text-muted-foreground tabular-nums w-10 text-right">
                {Math.round(progress)}%
              </span>
            </div>
          </div>
        )}

        {/* 动作按钮 */}
        {actions && actions.length > 0 && (
          <div className="mt-3 flex items-center gap-2 pl-6">
            {actions.map((action) => (
              <Button
                key={action.id}
                variant={action.variant ?? 'outline'}
                size="sm"
                className="text-xs h-7"
                onClick={(e) => handleAction(e, action)}
              >
                {action.label}
              </Button>
            ))}
          </div>
        )}

        {/* 阻塞式确认按钮 */}
        {isBlocking && (!actions || actions.length === 0) && (
          <div className="mt-3 flex items-center justify-end gap-2 pl-6">
            <Button
              size="sm"
              className="text-xs h-8"
              onClick={(e) => {
                e.stopPropagation()
                onAction?.(id, { id: 'confirm', label: '确认', action: 'confirm' })
              }}
            >
              确认继续
            </Button>
          </div>
        )}

        {/* 人类交互提示 */}
        {hasInteraction && !isBlocking && (
          <div className="mt-2 pl-6">
            <span className="text-xs text-primary font-medium">💬 点击打开交互面板</span>
          </div>
        )}
      </div>
    </div>
  )
}
