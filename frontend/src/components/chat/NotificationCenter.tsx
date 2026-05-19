/**
 * NotificationCenter - 通知中心面板
 *
 * 功能：
 * - 通知列表（按优先级分组、排序）
 * - 折叠/展开（低优先级可折叠，高优先级始终展开）
 * - 高优先级通知醒目样式
 * - 通知计数 badge
 * - 阻塞式通知模态框
 * - 全部已读 / 清空操作
 */

import { Bell, BellOff, ChevronDown, ChevronRight, X } from 'lucide-react'
import { useCallback, useMemo } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { useNotificationStore } from '@/stores/notificationStore'
import { NOTIFICATION_PRIORITY_WEIGHT, PRIORITY_STYLES } from '@/types/notification'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import { NotificationItemComponent } from './NotificationItem'
import type { NotificationAction, NotificationItem, NotificationPriority } from '@/types/notification'

/** 优先级分组标签 */
const PRIORITY_LABELS: Record<NotificationPriority, { label: string; emoji: string }> = {
  critical: { label: '紧急', emoji: '🔴' },
  high: { label: '重要', emoji: '🟠' },
  normal: { label: '普通', emoji: '🔵' },
  low: { label: '低优先', emoji: '⚪' },
}

/** 优先级排序顺序（用于分组渲染） */
const PRIORITY_ORDER: NotificationPriority[] = ['critical', 'high', 'normal', 'low']

export interface NotificationCenterProps {
  /** 自定义类名 */
  className?: string
}

export function NotificationCenter({ className }: NotificationCenterProps) {
  const notifications = useNotificationStore((s) => s.notifications)
  const groupState = useNotificationStore((s) => s.groupState)
  const isPanelOpen = useNotificationStore((s) => s.isPanelOpen)
  const activeBlockingNotification = useNotificationStore((s) => s.activeBlockingNotification)
  const togglePanel = useNotificationStore((s) => s.togglePanel)
  const closePanel = useNotificationStore((s) => s.closePanel)
  const dismissNotification = useNotificationStore((s) => s.dismissNotification)
  const markAsRead = useNotificationStore((s) => s.markAsRead)
  const markAllAsRead = useNotificationStore((s) => s.markAllAsRead)
  const clearAll = useNotificationStore((s) => s.clearAll)
  const toggleGroupCollapsed = useNotificationStore((s) => s.toggleGroupCollapsed)
  const confirmBlockingNotification = useNotificationStore((s) => s.confirmBlockingNotification)
  const executeAction = useNotificationStore((s) => s.executeAction)

  const unreadCount = useNotificationStore((s) => s.notifications.filter((n) => !n.isRead).length)

  /** 按优先级分组 */
  const groupedNotifications = useMemo(() => {
    const groups: Record<NotificationPriority, NotificationItem[]> = {
      critical: [],
      high: [],
      normal: [],
      low: [],
    }
    for (const n of notifications) {
      groups[n.priority].push(n)
    }
    return groups
  }, [notifications])

  /** 是否有通知 */
  const hasNotifications = notifications.length > 0

  /** 处理通知点击（标记已读） */
  const handleNotificationClick = useCallback(
    (notification: NotificationItem) => {
      if (!notification.isRead) {
        markAsRead(notification.id)
      }
    },
    [markAsRead],
  )

  /** 处理动作执行 */
  const handleAction = useCallback(
    (notificationId: string, action: NotificationAction) => {
      executeAction(notificationId, action)
    },
    [executeAction],
  )

  /** 渲染阻塞式通知模态框 */
  const renderBlockingDialog = () => {
    if (!activeBlockingNotification) return null

    return (
      <Dialog open={!!activeBlockingNotification} onOpenChange={() => {}}>
        <DialogContent className="border-red-500/50 sm:max-w-md" onPointerDownOutside={(e) => e.preventDefault()}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-red-600">
              <span>⚠️</span>
              <span>{activeBlockingNotification.title}</span>
            </DialogTitle>
            <DialogDescription className="text-sm text-muted-foreground">
              {activeBlockingNotification.message
                ? <MarkdownRenderer content={activeBlockingNotification.message} />
                : '请确认后继续执行'}
            </DialogDescription>
          </DialogHeader>

          {/* 进度条 */}
          {activeBlockingNotification.category === 'progress' &&
            activeBlockingNotification.progress != null && (
              <div className="py-2">
                <div className="h-2 w-full rounded-full bg-muted overflow-hidden">
                  <div
                    className="h-full rounded-full bg-primary transition-all duration-300"
                    style={{
                      width: `${Math.min(100, Math.max(0, activeBlockingNotification.progress))}%`,
                    }}
                  />
                </div>
                <p className="text-xs text-muted-foreground mt-1 text-right">
                  {Math.round(activeBlockingNotification.progress)}%
                </p>
              </div>
            )}

          {/* 动作按钮 */}
          {activeBlockingNotification.actions && activeBlockingNotification.actions.length > 0 && (
            <div className="flex flex-wrap gap-2 py-2">
              {activeBlockingNotification.actions.map((action) => (
                <Button
                  key={action.id}
                  variant={action.variant ?? 'outline'}
                  size="sm"
                  onClick={() => confirmBlockingNotification(action.id)}
                >
                  {action.label}
                </Button>
              ))}
            </div>
          )}

          <DialogFooter>
            <Button onClick={() => confirmBlockingNotification()}>确认继续</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  /** 渲染通知触发按钮（带未读计数 badge） */
  const renderTrigger = () => (
    <Button
      variant="ghost"
      size="sm"
      className={cn('relative h-8 w-8 p-0 rounded-full', unreadCount > 0 && 'text-primary')}
      onClick={togglePanel}
      aria-label={`通知中心${unreadCount > 0 ? ` (${unreadCount} 条未读)` : ''}`}
      data-testid="notification-center-trigger"
    >
      {unreadCount > 0 ? <Bell className="h-4 w-4" /> : <BellOff className="h-4 w-4 opacity-50" />}
      {unreadCount > 0 && (
        <span className="absolute -top-1 -right-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
    </Button>
  )

  /** 渲染分组 */
  const renderGroup = (priority: NotificationPriority) => {
    const items = groupedNotifications[priority]
    if (items.length === 0) return null

    const collapsed = groupState.collapsed[priority]
    const style = PRIORITY_STYLES[priority]
    const labelInfo = PRIORITY_LABELS[priority]
    const unreadInGroup = items.filter((n) => !n.isRead).length

    return (
      <div key={priority} className="mb-2">
        {/* 分组标题栏 */}
        <button
          className={cn(
            'flex w-full items-center gap-2 px-2 py-1.5 rounded-lg text-xs font-medium',
            'hover:bg-muted/50 transition-colors',
            style.text,
          )}
          onClick={() => toggleGroupCollapsed(priority)}
          data-testid={`notification-group-${priority}`}
        >
          {collapsed ? (
            <ChevronRight className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" />
          )}
          <span>
            {labelInfo.emoji} {labelInfo.label}
          </span>
          <span className="text-muted-foreground ml-1">({items.length})</span>
          {unreadInGroup > 0 && (
            <span className="ml-auto bg-primary/10 text-primary px-1.5 py-0.5 rounded-full text-[10px]">
              {unreadInGroup} 条未读
            </span>
          )}
        </button>

        {/* 通知列表（折叠时只显示摘要行） */}
        {!collapsed ? (
          <div className="space-y-1.5 mt-1 ml-1">
            {items.map((notification) => (
              <NotificationItemComponent
                key={notification.id}
                notification={notification}
                isCollapsed={false}
                onClick={handleNotificationClick}
                onDismiss={dismissNotification}
                onAction={handleAction}
                className="group"
              />
            ))}
          </div>
        ) : (
          /* 折叠摘要 */
          items.length > 0 && (
            <div className="space-y-0.5 mt-1 ml-1">
              {items.slice(0, 2).map((notification) => (
                <NotificationItemComponent
                  key={notification.id}
                  notification={notification}
                  isCollapsed={true}
                  onClick={handleNotificationClick}
                  onDismiss={dismissNotification}
                  onAction={handleAction}
                />
              ))}
              {items.length > 2 && (
                <button
                  className="text-xs text-muted-foreground hover:text-primary px-3 py-1 transition-colors"
                  onClick={() => toggleGroupCollapsed(priority)}
                >
                  还有 {items.length - 2} 条{labelInfo.label}通知...
                </button>
              )}
            </div>
          )
        )}
      </div>
    )
  }

  return (
    <>
      {/* 阻塞式通知模态框（全局层，不依赖面板状态） */}
      {renderBlockingDialog()}

      {/* 通知触发按钮 */}
      {renderTrigger()}

      {/* 通知中心面板 */}
      {isPanelOpen && (
        <div
          className={cn(
            'absolute top-full right-0 mt-2 w-96 max-h-[70vh]',
            'bg-background border border-border rounded-xl shadow-xl',
            'z-50 flex flex-col overflow-hidden',
            'animate-in slide-in-from-top-2 duration-200',
            className,
          )}
          data-testid="notification-center-panel"
        >
          {/* 面板头部 */}
          <div className="flex items-center justify-between px-4 py-3 border-b">
            <div className="flex items-center gap-2">
              <Bell className="h-4 w-4" />
              <span className="text-sm font-semibold">通知中心</span>
              {unreadCount > 0 && (
                <span className="bg-red-500 text-white text-[10px] px-1.5 py-0.5 rounded-full">
                  {unreadCount}
                </span>
              )}
            </div>
            <div className="flex items-center gap-1">
              {hasNotifications && (
                <>
                  <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={markAllAsRead}>
                    全部已读
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 text-xs text-destructive hover:text-destructive"
                    onClick={clearAll}
                  >
                    清空
                  </Button>
                </>
              )}
              <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={closePanel}>
                <X className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>

          {/* 面板内容 */}
          <div className="flex-1 min-h-0 overflow-y-auto p-3">
            {hasNotifications ? (
              PRIORITY_ORDER.map(renderGroup)
            ) : (
              <div className="flex flex-col items-center justify-center py-8 text-muted-foreground">
                <BellOff className="h-8 w-8 mb-2 opacity-30" />
                <p className="text-sm">暂无通知</p>
              </div>
            )}
          </div>
        </div>
      )}
    </>
  )
}
