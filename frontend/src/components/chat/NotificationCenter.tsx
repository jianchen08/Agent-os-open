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
 *
 * 滚动处理：body/#root 的 overflow:hidden 会让浏览器合成器线程在处理真实鼠标滚轮时
 * 直接消费掉事件、不生成 JS wheel 事件。因此面板打开时临时把 body overflow 改为
 * 'visible'，同时阻止 wheel 事件冒泡到 body 防止触发页面抖动；面板关闭时恢复
 * 原始 overflow 值。
 */

import { Bell, BellOff, ChevronDown, ChevronRight, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { createPortal } from 'react-dom'
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
import { useInteractionStore } from '@/stores/interactionStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { PRIORITY_STYLES } from '@/types/notification'
import { MarkdownRenderer } from './markdown/MarkdownRenderer'
import { NotificationItemComponent } from './NotificationItem'
import type { NotificationAction, NotificationItem, NotificationPriority } from '@/types/notification'

const PRIORITY_LABELS: Record<NotificationPriority, { label: string; emoji: string }> = {
  critical: { label: '紧急', emoji: '🔴' },
  high: { label: '重要', emoji: '🟠' },
  normal: { label: '普通', emoji: '🔵' },
  low: { label: '低优先', emoji: '⚪' },
}

const PRIORITY_ORDER: NotificationPriority[] = ['critical', 'high', 'normal', 'low']

export interface NotificationCenterProps {
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

  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

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

  const hasNotifications = notifications.length > 0

  const handleNotificationClick = useCallback(
    (notification: NotificationItem) => {
      if (!notification.isRead) {
        markAsRead(notification.id)
      }
      const sourceId = (notification as any).sourceId as string | undefined
      if (sourceId) {
        const interaction = useInteractionStore
          .getState()
          .pendingInteractions.find((i) => i.requestId === sourceId)
        if (interaction && interaction.status === 'pending') {
          useInteractionStore.getState().setGlobalOpenRequestId(sourceId)
          closePanel()
          return
        }
      }
    },
    [markAsRead, closePanel],
  )

  const handleAction = useCallback(
    (notificationId: string, action: NotificationAction) => {
      executeAction(notificationId, action)
    },
    [executeAction],
  )

  /**
   * 面板打开时：临时解除 body overflow:hidden + 阻止 wheel 冒泡
   * 面板关闭时：恢复 body 原始 overflow
   *
   * 原理：浏览器合成器线程看到 body overflow:hidden 后会直接消费真实滚轮事件，
   * 不会传递到 JS 层。临时改为 overflow:visible 后合成器允许事件传递。
   * 同时阻止 wheel 冒泡，防止页面内容跟着滚动。
   */
  useEffect(() => {
    if (!isPanelOpen) return

    const originalOverflow = document.body.style.overflow
    const originalRootOverflow = document.documentElement.style.overflow

    document.body.style.overflow = 'visible'
    document.documentElement.style.overflow = 'visible'

    const stopWheelPropagation = (e: WheelEvent) => {
      if (panelRef.current?.contains(e.target as Node)) {
        e.stopPropagation()
      }
    }

    document.addEventListener('wheel', stopWheelPropagation, true)

    return () => {
      document.body.style.overflow = originalOverflow
      document.documentElement.style.overflow = originalRootOverflow
      document.removeEventListener('wheel', stopWheelPropagation, true)
    }
  }, [isPanelOpen])

  /** 点击面板外部关闭 */
  useEffect(() => {
    if (!isPanelOpen) return

    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current?.contains(target)) return
      closePanel()
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isPanelOpen, closePanel])

  /** ESC 关闭面板 */
  useEffect(() => {
    if (!isPanelOpen) return

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePanel()
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isPanelOpen, closePanel])

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
      ref={triggerRef}
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
                hasInteraction={!!(notification as any).sourceId}
                className="group"
              />
            ))}
          </div>
        ) : (
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
                  hasInteraction={!!(notification as any).sourceId}
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
      {renderBlockingDialog()}
      {renderTrigger()}

      {isPanelOpen && createPortal(
        <>
          <div
            className="fixed inset-0 bg-black/10"
            style={{ zIndex: 9998 }}
            onClick={closePanel}
            data-testid="notification-overlay"
          />
          <div
            ref={panelRef}
            className={cn(
              'fixed right-0 top-0',
              'border-l border-border shadow-2xl',
              'flex flex-col',
              className,
            )}
            style={{
              zIndex: 9999,
              width: '400px',
              maxWidth: '85vw',
              height: '100vh',
              backgroundColor: 'hsl(var(--panel-solid))',
            }}
            data-testid="notification-center-panel"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b shrink-0">
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

            <div
              className="flex-1 overflow-y-auto p-3"
              style={{ minHeight: 0 }}
              data-testid="notification-scroll-area"
            >
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
        </>,
        document.body,
      )}
    </>
  )
}
