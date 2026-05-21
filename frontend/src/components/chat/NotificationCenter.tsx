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
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
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

  /** 触发按钮引用，用于计算面板定位 */
  const triggerRef = useRef<HTMLButtonElement>(null)

  /**
   * BUG-FIX-fix_20260521_notification_scroll:
   * 面板定位坐标（fixed 定位，通过 Portal 渲染到 body）
   *
   * 问题根因: ChatContainer 根元素有 overflow-hidden，通知面板使用 absolute top-full
   *          向下展开时被裁剪，导致面板只有部分可见、无法滚动。
   * 修复方案: 使用 createPortal 将面板渲染到 document.body，采用 fixed 定位，
   *          根据 trigger 按钮的 getBoundingClientRect 计算面板坐标，
   *          彻底脱离 ChatContainer 的 overflow-hidden 上下文。
   * 影响范围: 通知中心面板的显示与滚动
   * 修复日期: 2026-05-21
   */
  const [panelPosition, setPanelPosition] = useState({ top: 0, right: 0 })

  /** 计算面板定位（面板打开时同步 trigger 按钮位置） */
  useEffect(() => {
    if (!isPanelOpen || !triggerRef.current) return

    const updatePosition = () => {
      if (!triggerRef.current) return
      const rect = triggerRef.current.getBoundingClientRect()
      setPanelPosition({
        top: rect.bottom + 8,
        right: window.innerWidth - rect.right,
      })
    }

    updatePosition()
    window.addEventListener('resize', updatePosition)
    window.addEventListener('scroll', updatePosition, true)
    return () => {
      window.removeEventListener('resize', updatePosition)
      window.removeEventListener('scroll', updatePosition, true)
    }
  }, [isPanelOpen])

  /** 面板 DOM 引用，用于点击外部关闭 */
  const panelRef = useRef<HTMLDivElement>(null)

  /** 点击面板外部关闭面板 */
  useEffect(() => {
    if (!isPanelOpen) return

    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node
      // 如果点击的是 trigger 按钮本身，由 togglePanel 处理
      if (triggerRef.current?.contains(target)) return
      // 如果点击在面板内部，不关闭
      if (panelRef.current?.contains(target)) return
      closePanel()
    }

    // 使用 mousedown 而非 click，避免拖拽选择文本时误触
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isPanelOpen, closePanel])

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

      {/* 通知中心面板 - 通过 Portal 渲染到 body，避免被父容器 overflow-hidden 裁剪 */}
      {isPanelOpen && createPortal(
        <div
          ref={panelRef}
          className={cn(
            'fixed w-96 max-h-[70vh]',
            'bg-background border border-border rounded-xl shadow-xl',
            'z-[9999] flex flex-col overflow-hidden',
            'animate-in slide-in-from-top-2 duration-200',
            className,
          )}
          style={{
            top: `${panelPosition.top}px`,
            right: `${panelPosition.right}px`,
          }}
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
        </div>,
        document.body,
      )}
    </>
  )
}
