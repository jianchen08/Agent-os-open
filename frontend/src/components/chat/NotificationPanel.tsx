/**
 * NotificationPanel - 可滚动的通知面板组件
 *
 * 解决两个核心问题：
 * 1. 通知太多时用户看不到底部的通知 → 列表容器限制最大高度 + 滚动
 * 2. 单条通知内容太长时撑开整个面板 → 单条通知限制最大高度 + 内容截断
 *
 * 从 notificationStore 消费 notifications 数据，
 * 复用 NotificationItemComponent 渲染每条通知。
 */

import { BellOff } from 'lucide-react'
import { useCallback } from 'react'
import { cn } from '@/lib/utils'
import { useNotificationStore } from '@/stores/notificationStore'
import type { NotificationAction, NotificationItem } from '@/types/notification'
import { NotificationItemComponent } from './NotificationItem'

/** 列表容器的默认最大高度 */
const DEFAULT_LIST_MAX_HEIGHT = '60vh'

/** 单条通知的默认最大高度 */
const DEFAULT_ITEM_MAX_HEIGHT = 200

export interface NotificationPanelProps {
  /** 列表容器的最大高度（CSS 值），默认 '60vh' */
  listMaxHeight?: string
  /** 单条通知的最大高度（px），默认 200 */
  itemMaxHeight?: number
  /** 自定义类名 */
  className?: string
}

export function NotificationPanel({
  listMaxHeight = DEFAULT_LIST_MAX_HEIGHT,
  itemMaxHeight = DEFAULT_ITEM_MAX_HEIGHT,
  className,
}: NotificationPanelProps) {
  const notifications = useNotificationStore((s) => s.notifications)
  const markAsRead = useNotificationStore((s) => s.markAsRead)
  const dismissNotification = useNotificationStore((s) => s.dismissNotification)
  const executeAction = useNotificationStore((s) => s.executeAction)

  /** 点击通知标记已读 */
  const handleNotificationClick = useCallback(
    (notification: NotificationItem) => {
      if (!notification.isRead) {
        markAsRead(notification.id)
      }
    },
    [markAsRead],
  )

  /** 执行通知动作 */
  const handleAction = useCallback(
    (notificationId: string, action: NotificationAction) => {
      executeAction(notificationId, action)
    },
    [executeAction],
  )

  /** 空状态 */
  if (notifications.length === 0) {
    return (
      <div
        className={cn(
          'flex flex-col items-center justify-center py-8 text-muted-foreground',
          className,
        )}
        data-testid="notification-panel-empty"
      >
        <BellOff className="h-8 w-8 mb-2 opacity-30" />
        <p className="text-sm">暂无通知</p>
      </div>
    )
  }

  return (
    <div
      className={cn('overflow-y-auto', className)}
      style={{ maxHeight: listMaxHeight }}
      data-testid="notification-panel-list"
    >
      <div className="space-y-2">
        {notifications.map((notification) => (
          <div
            key={notification.id}
            className="overflow-y-auto"
            style={{ maxHeight: itemMaxHeight }}
            data-testid={`notification-panel-item-wrapper-${notification.id}`}
          >
            <NotificationItemComponent
              notification={notification}
              isCollapsed={false}
              onClick={handleNotificationClick}
              onDismiss={dismissNotification}
              onAction={handleAction}
              className="group"
            />
          </div>
        ))}
      </div>
    </div>
  )
}
