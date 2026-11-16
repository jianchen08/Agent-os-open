/**
 * NotificationCenter - 通知中心面板
 *
 * 功能：
 * - 通知列表（单列时间线，最新在最上；优先级只由条目自身颜色/图标体现）
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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Bell, BellOff, X } from '@/assets/icons'
import { runLayoutAlertAction, type LayoutAlertActionPayload } from '@/components/layout/LayoutAlertNotifications'
import { ConfirmDialog } from '@/components/shared/ConfirmDialog'
import { MarkdownRenderer } from '@/components/shared/markdown/MarkdownRenderer'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import { cn } from '@/lib/utils'
import { openWorkspacePanel, TOP_NAV_PANELS } from '@/services/workspacePanelOpener'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import {
  resolveNotificationRoute,
  sortNotificationsNewestFirst,
  useNotificationStore,
} from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useConfirmDialog } from '@/utils/confirm'
import { resolveNotificationLayout } from '@/utils/notificationModes'
import { NotificationItemComponent } from './NotificationItem'
import type { NotificationAction, NotificationItem } from '@/types/notification'

export interface NotificationCenterProps {
  className?: string
  /** P5：隐藏自带触发按钮（由侧边栏 Bell 作为唯一入口触发 togglePanel） */
  hideTrigger?: boolean
}

/**
 * 渐进渲染（P-145）：列表 cap=200 后单面板全量 DOM 仍可优化——初始只渲染
 * 最近 50 条，「显示更早」每点一批 +50 分批展开至全量。低于阈值零行为变化
 * （无按钮、全量直渲）；分组/已读/清空/点击路由不受影响。
 */
const INITIAL_VISIBLE_COUNT = 50
const EXPAND_BATCH_SIZE = 50

export function NotificationCenter({ className, hideTrigger = false }: NotificationCenterProps) {
  const notifications = useNotificationStore((s) => s.notifications)
  const isPanelOpen = useNotificationStore((s) => s.isPanelOpen)
  const activeBlockingNotification = useNotificationStore((s) => s.activeBlockingNotification)
  const togglePanel = useNotificationStore((s) => s.togglePanel)
  const closePanel = useNotificationStore((s) => s.closePanel)
  const dismissNotification = useNotificationStore((s) => s.dismissNotification)
  const markAsRead = useNotificationStore((s) => s.markAsRead)
  const markAllAsRead = useNotificationStore((s) => s.markAllAsRead)
  const clearAll = useNotificationStore((s) => s.clearAll)
  const confirmBlockingNotification = useNotificationStore((s) => s.confirmBlockingNotification)
  const executeAction = useNotificationStore((s) => s.executeAction)

  const unreadCount = useNotificationStore((s) => s.notifications.filter((n) => !n.isRead).length)

  const triggerRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)

  // X18-F1（P-137）：清空不可恢复，先过 DOM 可见确认层（共享 ConfirmDialog；
  // 原生 confirm 在自动化浏览器被静默 auto-dismiss，BUG-23）
  const { confirm: confirmClear, dialogState: clearConfirmState } = useConfirmDialog()
  const clearConfirmRef = useRef<HTMLDivElement>(null)
  const handleClearAll = useCallback(async () => {
    if (!(await confirmClear('确定清空全部通知？清空后不可恢复。'))) return
    clearAll()
  }, [confirmClear, clearAll])

  /** 面板列表：时间倒序（最新在最上），不按优先级分组 */
  const sortedNotifications = useMemo(
    () => sortNotificationsNewestFirst(notifications),
    [notifications],
  )

  /** 渐进渲染（P-145）：当前可见条目数；面板每次打开回落到初始阈值（展开态不跨开合保留） */
  const [visibleCount, setVisibleCount] = useState(INITIAL_VISIBLE_COUNT)
  useEffect(() => {
    if (isPanelOpen) setVisibleCount(INITIAL_VISIBLE_COUNT)
  }, [isPanelOpen])

  const visibleNotifications = useMemo(
    () => sortedNotifications.slice(0, visibleCount),
    [sortedNotifications, visibleCount],
  )
  const olderRemaining = sortedNotifications.length - visibleNotifications.length

  const hasNotifications = notifications.length > 0

  /** 会话坐标路由（条目点击与失败卡「重新发送」动作共用）：会话在列表中才
   * 切换到该会话并关闭面板。会话 id 坐标可能是管道 id 等非会话键（如命中率
   * 告警按 pipeline 挂 sessionId）：不在会话列表一律视为无路由坐标，维持现状
   * 不跳转。 */
  const routeToSession = useCallback(
    (sessionId: string) => {
      if (readSessions().some((s) => s.id === sessionId)) {
        void useSessionListStore.getState().setActiveSession(sessionId)
        closePanel()
      }
    },
    [closePanel],
  )

  const handleNotificationClick = useCallback(
    (notification: NotificationItem) => {
      if (!notification.isRead) {
        markAsRead(notification.id)
      }
      const sourceId = notification.sourceId
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
      // 按 payload 路由（OBS-R259-1）：任务 → 打开/激活任务管理签并定位该任务；
      // 会话 → 切换到该会话；都无（或会话已不在列表）→ 维持现状（零导航、面板不动）
      const route = resolveNotificationRoute(notification)
      if (!route) return
      if (route.kind === 'task') {
        const spec = TOP_NAV_PANELS['/tasks']
        openWorkspacePanel(spec)
        useLayoutModeStore
          .getState()
          .updateWorkspaceTab(spec.id, { props: { focusTaskId: route.taskId } })
        closePanel()
        return
      }
      routeToSession(route.sessionId)
    },
    [markAsRead, closePanel, routeToSession],
  )

  const handleAction = useCallback(
    (notificationId: string, action: NotificationAction) => {
      // 系统异常条目的动作（合并一：原顶栏横幅动作并入通知面）——payload 携带
      // layoutAlert 动作声明时分发到告警动作执行器（reconnect/open-page）；条目
      // 由条件同步收尾（异常解除自动移除），动作本身不消除条目
      const payload = action.payload as
        | { layoutAlert?: LayoutAlertActionPayload; retrySessionId?: unknown }
        | undefined
      if (action.action === 'custom' && payload?.layoutAlert) {
        runLayoutAlertAction(payload.layoutAlert)
        return
      }
      // 失败卡「重新发送」动作（P-26）：payload 携带 retrySessionId 时复用既有
      // 会话路由定位到对应会话（重发是用户语义动作，只定位不代发）；动作本身
      // 不消除条目，与 layoutAlert 分发同语义
      if (action.action === 'custom' && typeof payload?.retrySessionId === 'string') {
        routeToSession(payload.retrySessionId)
        return
      }
      executeAction(notificationId, action)
    },
    [executeAction, routeToSession],
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

  /** 点击面板外部关闭（清空确认层打开期间不关：确认层全屏覆盖，点它只走确认/取消） */
  useEffect(() => {
    if (!isPanelOpen) return

    const handleClickOutside = (e: MouseEvent) => {
      const target = e.target as Node
      if (triggerRef.current?.contains(target)) return
      if (panelRef.current?.contains(target)) return
      if (clearConfirmRef.current?.contains(target)) return
      closePanel()
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [isPanelOpen, closePanel])

  /** ESC 关闭面板（清空确认层打开期间保留现场，由确认/取消/遮罩收尾） */
  useEffect(() => {
    if (!isPanelOpen) return

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (clearConfirmState.open) return
        closePanel()
      }
    }

    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [isPanelOpen, closePanel, clearConfirmState.open])

  /** 渲染阻塞式通知模态框 */
  const renderBlockingDialog = () => {
    if (!activeBlockingNotification) return null

    return (
      <Dialog open={!!activeBlockingNotification} onOpenChange={() => {}}>
        <DialogContent className="border-status-error/50/50 sm:max-w-md" onPointerDownOutside={(e) => e.preventDefault()}>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-status-error">
              <span>⚠️</span>
              <span>{activeBlockingNotification.title}</span>
            </DialogTitle>
            <DialogDescription className="text-sm text-muted-foreground">
              {activeBlockingNotification.message
                ? <MarkdownRenderer content={activeBlockingNotification.message} />
                : '请确认后继续执行'}
            </DialogDescription>
          </DialogHeader>

          {resolveNotificationLayout(activeBlockingNotification).features.has('progress') &&
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
      {unreadCount > 0 ? <Bell className="h-icon-md w-icon-md" /> : <BellOff className="h-icon-md w-icon-md opacity-50" />}
      {unreadCount > 0 && (
        <span className="absolute -top-1 -right-1 flex h-icon-md min-w-icon-md items-center justify-center rounded-full bg-status-error px-1 text-[10px] font-bold text-status-error-foreground">
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
    </Button>
  )

  return (
    <>
      {renderBlockingDialog()}
      {/* P5: hideTrigger 时隐藏自带触发按钮（由侧边栏 Bell 作为唯一入口调用 togglePanel） */}
      {!hideTrigger && renderTrigger()}

      {/* 清空二次确认层（X18-F1）：ConfirmDialog 固定 z-50 会被 z=9999 的面板
          盖住，经定位包裹层抬到面板之上；ref 同时供面板外点关闭守卫判含 */}
      <div className="relative z-[10000]" ref={clearConfirmRef}>
        <ConfirmDialog dialogState={clearConfirmState} />
      </div>

      {isPanelOpen && createPortal(
        <>
          <div
            className="fixed inset-0 bg-[var(--overlay-subtle)]"
            style={{ zIndex: 9998 }}
            onClick={closePanel}
            data-testid="notification-overlay"
          />
          <div
            ref={panelRef}
            className={cn(
              'fixed right-0',
              'border-l border-border shadow-2xl',
              'flex flex-col',
              className,
            )}
            style={{
              zIndex: 9999,
              width: '400px',
              maxWidth: '85vw',
              // 全屏沉浸下面板直通屏幕顶：标题行/关闭钮须避让状态栏；底部
              // 同扣手势条位（末条通知不被遮挡）。桌面变量恒 0 行为不变。
              top: 'var(--safe-area-top, 0px)',
              height: 'calc(100dvh - var(--safe-area-top, 0px) - var(--safe-area-bottom, 0px))',
              backgroundColor: 'hsl(var(--panel-solid))',
            }}
            data-testid="notification-center-panel"
          >
            <div className="flex items-center justify-between px-4 py-3 border-b shrink-0">
              <div className="flex items-center gap-2">
                <Bell className="h-icon-md w-icon-md" />
                <span className="text-sm font-semibold">通知中心</span>
                {unreadCount > 0 && (
                  <span className="bg-status-error text-status-error-foreground text-[10px] px-1.5 py-0.5 rounded-full">
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
                      onClick={() => void handleClearAll()}
                    >
                      清空
                    </Button>
                  </>
                )}
                <Button variant="ghost" size="sm" className="h-7 w-7 p-0" onClick={closePanel}>
                  <X className="h-icon-sm w-icon-sm" />
                </Button>
              </div>
            </div>

            <div
              className="flex-1 overflow-y-auto p-3"
              style={{ minHeight: 0 }}
              data-testid="notification-scroll-area"
            >
              {hasNotifications ? (
                <>
                  <div className="space-y-1.5" data-testid="notification-list">
                    {visibleNotifications.map((notification) => (
                      <NotificationItemComponent
                        key={notification.id}
                        notification={notification}
                        onClick={handleNotificationClick}
                        onDismiss={dismissNotification}
                        onAction={handleAction}
                        hasInteraction={!!notification.sourceId}
                        className="group"
                      />
                    ))}
                  </div>
                  {olderRemaining > 0 && (
                    <div className="flex justify-center pt-2">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 text-xs text-muted-foreground"
                        onClick={() => setVisibleCount((c) => c + EXPAND_BATCH_SIZE)}
                        data-testid="notification-show-older"
                      >
                        显示更早（还有 {olderRemaining} 条）
                      </Button>
                    </div>
                  )}
                </>
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
