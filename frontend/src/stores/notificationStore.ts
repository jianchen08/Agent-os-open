/**
 * 通知状态管理 Store
 *
 * 管理非阻塞/阻塞通知队列、优先级排序、折叠展开状态。
 * 纯状态层，不涉及通信或 UI。
 */

import { create } from 'zustand'
import { NOTIFICATION_PRIORITY_WEIGHT } from '@/types/notification'
import type {
  NotificationItem,
  NotificationPriority,
  NotificationGroupState,
  NotificationAction,
} from '@/types/notification'

/** 自动递增 ID 计数器 */
let _nextId = 1

/** 生成唯一通知 ID */
function generateNotificationId(): string {
  return `notif-${Date.now()}-${_nextId++}`
}

/** 通知中心状态接口 */
interface NotificationState {
  /** 通知列表（按优先级排序） */
  notifications: NotificationItem[]
  /** 折叠状态 */
  groupState: NotificationGroupState
  /** 通知中心面板是否展开 */
  isPanelOpen: boolean
  /** 当前阻塞式通知（仅一条，模态展示） */
  activeBlockingNotification: NotificationItem | null

  // ---- Actions ----

  /** 添加通知 */
  addNotification: (data: Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'> & { id?: string }) => string
  /** 批量添加通知 */
  addNotifications: (items: Array<Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'>>) => string[]
  /** 移除通知 */
  removeNotification: (id: string) => void
  /** 标记已读 */
  markAsRead: (id: string) => void
  /** 标记全部已读 */
  markAllAsRead: () => void
  /** 清除所有通知 */
  clearAll: () => void
  /** 忽略（移除）通知 */
  dismissNotification: (id: string) => void
  /** 更新通知进度 */
  updateProgress: (id: string, progress: number) => void
  /** 将非阻塞通知升级为阻塞 */
  escalateToBlocking: (id: string) => void
  /** 确认阻塞式通知（关闭并移除） */
  confirmBlockingNotification: (actionId?: string) => void
  /** 执行通知动作 */
  executeAction: (notificationId: string, action: NotificationAction) => void
  /** 切换折叠状态 */
  toggleGroupCollapsed: (priority: NotificationPriority) => void
  /** 切换面板展开/收起 */
  togglePanel: () => void
  /** 打开面板 */
  openPanel: () => void
  /** 关闭面板 */
  closePanel: () => void
  /** 获取未读计数 */
  getUnreadCount: () => number
  /** 获取指定优先级的通知列表 */
  getByPriority: (priority: NotificationPriority) => NotificationItem[]
  /** 获取指定优先级的未读通知数量 */
  getUnreadCountByPriority: (priority: NotificationPriority) => number
}

/** 默认折叠状态 */
const DEFAULT_GROUP_STATE: NotificationGroupState = {
  collapsed: {
    critical: false,
    high: false,
    normal: true,
    low: true,
  },
}

/**
 * 排序通知：优先级降序 + 时间升序（同优先级先到先排）
 */
function sortNotifications(items: NotificationItem[]): NotificationItem[] {
  return [...items].sort((a, b) => {
    const weightDiff =
      (NOTIFICATION_PRIORITY_WEIGHT[b.priority] ?? 2) -
      (NOTIFICATION_PRIORITY_WEIGHT[a.priority] ?? 2)
    if (weightDiff !== 0) return weightDiff
    return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
  })
}

/**
 * 检查是否为新的阻塞通知，如是则设为 activeBlockingNotification
 */
function checkBlockingNotification(
  state: { activeBlockingNotification: NotificationItem | null },
  newItem: NotificationItem,
): Partial<NotificationState> {
  if (newItem.isBlocking && !state.activeBlockingNotification) {
    return { activeBlockingNotification: newItem }
  }
  return {}
}

export const useNotificationStore = create<NotificationState>()((set, get) => ({
  notifications: [],
  groupState: { ...DEFAULT_GROUP_STATE },
  isPanelOpen: false,
  activeBlockingNotification: null,

  addNotification: (data) => {
    const id = data.id ?? generateNotificationId()
    const newItem: NotificationItem = {
      ...data,
      id,
      isRead: false,
      timestamp: new Date().toISOString(),
    }

    set((state) => {
      if (state.notifications.some((n) => n.id === id)) return state

      const updated = sortNotifications([...state.notifications, newItem])
      const blockingUpdate = checkBlockingNotification(state, newItem)

      const shouldAutoOpen =
        !state.isPanelOpen &&
        (newItem.priority === 'high' || newItem.priority === 'critical')

      return {
        notifications: updated,
        ...blockingUpdate,
        ...(shouldAutoOpen ? { isPanelOpen: true } : {}),
      }
    })

    if (data.autoDismissMs && data.autoDismissMs > 0 && !data.isBlocking) {
      setTimeout(() => {
        get().dismissNotification(id)
      }, data.autoDismissMs)
    }

    return id
  },

  addNotifications: (items) => {
    const ids: string[] = []
    const newItems: NotificationItem[] = []

    for (const data of items) {
      const id = generateNotificationId()
      ids.push(id)
      newItems.push({
        ...data,
        id,
        isRead: false,
        timestamp: new Date().toISOString(),
      })
    }

    set((state) => {
      const updated = sortNotifications([...state.notifications, ...newItems])
      // 查找第一个阻塞通知
      const firstBlocking = newItems.find((n) => n.isBlocking)
      const blockingUpdate =
        firstBlocking && !state.activeBlockingNotification
          ? { activeBlockingNotification: firstBlocking }
          : {}

      return {
        notifications: updated,
        ...blockingUpdate,
      }
    })

    return ids
  },

  removeNotification: (id) => {
    set((state) => {
      const updated = state.notifications.filter((n) => n.id !== id)
      const blockingUpdate =
        state.activeBlockingNotification?.id === id
          ? { activeBlockingNotification: null }
          : {}
      return {
        notifications: updated,
        ...blockingUpdate,
      }
    })
  },

  markAsRead: (id) => {
    set((state) => ({
      notifications: state.notifications.map((n) =>
        n.id === id ? { ...n, isRead: true } : n,
      ),
    }))
  },

  markAllAsRead: () => {
    set((state) => ({
      notifications: state.notifications.map((n) => ({ ...n, isRead: true })),
    }))
  },

  clearAll: () => {
    set({
      notifications: [],
      activeBlockingNotification: null,
    })
  },

  dismissNotification: (id) => {
    get().removeNotification(id)
  },

  updateProgress: (id, progress) => {
    set((state) => ({
      notifications: state.notifications.map((n) =>
        n.id === id ? { ...n, progress: Math.min(100, Math.max(0, progress)) } : n,
      ),
      activeBlockingNotification:
        state.activeBlockingNotification?.id === id
          ? {
              ...state.activeBlockingNotification,
              progress: Math.min(100, Math.max(0, progress)),
            }
          : state.activeBlockingNotification,
    }))
  },

  escalateToBlocking: (id) => {
    set((state) => {
      const target = state.notifications.find((n) => n.id === id)
      if (!target) return state

      const updated = state.notifications.map((n) =>
        n.id === id ? { ...n, isBlocking: true, priority: 'high' as const } : n,
      )

      return {
        notifications: updated,
        activeBlockingNotification: state.activeBlockingNotification ?? {
          ...target,
          isBlocking: true,
          priority: 'high',
        },
      }
    })
  },

  confirmBlockingNotification: (actionId) => {
    const { activeBlockingNotification } = get()
    if (!activeBlockingNotification) return

    // 执行 confirm 动作（如有）
    if (actionId && activeBlockingNotification.actions) {
      const action = activeBlockingNotification.actions.find((a) => a.id === actionId)
      if (action) {
        get().executeAction(activeBlockingNotification.id, action)
        return
      }
    }

    // 默认行为：移除阻塞通知并标记已读
    set((state) => ({
      notifications: state.notifications.map((n) =>
        n.id === activeBlockingNotification.id ? { ...n, isBlocking: false, isRead: true } : n,
      ),
      activeBlockingNotification: null,
    }))
  },

  executeAction: (notificationId, action) => {
    switch (action.action) {
      case 'dismiss':
        get().dismissNotification(notificationId)
        break
      case 'confirm':
        get().confirmBlockingNotification(action.id)
        break
      case 'navigate':
        // 导航由组件层处理，此处仅标记已读
        get().markAsRead(notificationId)
        break
      case 'custom':
        // 自定义动作由组件层通过 payload 处理
        get().markAsRead(notificationId)
        break
    }
  },

  toggleGroupCollapsed: (priority) => {
    set((state) => ({
      groupState: {
        collapsed: {
          ...state.groupState.collapsed,
          [priority]: !state.groupState.collapsed[priority],
        },
      },
    }))
  },

  togglePanel: () => {
    set((state) => ({ isPanelOpen: !state.isPanelOpen }))
  },

  openPanel: () => {
    set({ isPanelOpen: true })
  },

  closePanel: () => {
    set({ isPanelOpen: false })
  },

  getUnreadCount: () => {
    return get().notifications.filter((n) => !n.isRead).length
  },

  getByPriority: (priority) => {
    return get().notifications.filter((n) => n.priority === priority)
  },

  getUnreadCountByPriority: (priority) => {
    return get().notifications.filter((n) => n.priority === priority && !n.isRead).length
  },
}))
