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

/**
 * 无 id 通知的内容指纹入列记录：fingerprint → { id, at }。
 * 重复投递的事件可能不携带稳定 id（纯前端错误提示等），按内容指纹兜底。
 */
const recentFingerprints = new Map<string, { id: string; at: number }>()

/**
 * 指纹去重窗口：必须覆盖重复投递的实际间隔抖动（/interaction/pending 兜底
 * 轮询周期 10s + 网络延迟），10s 整会因投递抖动漏挡，故取 30s。
 */
const FINGERPRINT_DEDUP_WINDOW_MS = 30_000

/** 已被用户移除/清空的通知 id 记忆上限（长期会话防无界增长） */
const DISMISSED_ID_CAP = 1000

/** 已被用户移除/清空的通知 id：同 id 重投不回弹（clear 后轮询/重放不得复活旧通知） */
const dismissedNotificationIds = new Set<string>()

function rememberDismissedId(id: string): void {
  dismissedNotificationIds.add(id)
  while (dismissedNotificationIds.size > DISMISSED_ID_CAP) {
    const oldest = dismissedNotificationIds.values().next().value
    if (oldest === undefined) break
    dismissedNotificationIds.delete(oldest)
  }
}

/**
 * 通知点击路由目标（按通知关联坐标解析的导航语义，数据面供面板点击路由消费）
 */
export type NotificationRouteTarget =
  | { kind: 'task'; taskId: string }
  | { kind: 'session'; sessionId: string }

/**
 * 按通知关联坐标解析点击路由目标（OBS-R259-1）：
 * task_id 优先（任务管理签定位），其次 thread/session（同源会话 id）；
 * 都无 → null（调用方维持现状，零导航）。
 */
export function resolveNotificationRoute(
  notification: Pick<NotificationItem, 'taskId' | 'sessionId'>,
): NotificationRouteTarget | null {
  if (notification.taskId) return { kind: 'task', taskId: notification.taskId }
  if (notification.sessionId) return { kind: 'session', sessionId: notification.sessionId }
  return null
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
    const now = new Date()
    const id = data.id ?? generateNotificationId()

    // 无 id 通知兜底闸：同 title+message 短窗内只入一条（重连重放/轮询重入
    // 类重复投递不携带稳定 id 时按内容指纹挡住刷屏）。命中时返回首次入列 id。
    // 携带关联坐标（sessionId/sourceId/taskId/agentId）的通知不走此闸：这类
    // 是按实体语义的重复事件（如逐 pipeline 的命中率骤降告警），短窗吞掉
    // 会让真实告警丢失——它们的重复投递应在生产方修复。
    if (!data.id && !data.sessionId && !data.sourceId && !data.taskId && !data.agentId) {
      const fingerprint = `${data.title}\u0000${data.message ?? ''}`
      const seen = recentFingerprints.get(fingerprint)
      if (seen && now.getTime() - seen.at < FINGERPRINT_DEDUP_WINDOW_MS) {
        return seen.id
      }
      recentFingerprints.set(fingerprint, { id, at: now.getTime() })
      for (const [key, value] of recentFingerprints) {
        if (now.getTime() - value.at >= FINGERPRINT_DEDUP_WINDOW_MS) {
          recentFingerprints.delete(key)
        }
      }
    }

    const newItem: NotificationItem = {
      ...data,
      id,
      isRead: false,
      timestamp: now.toISOString(),
    }

    set((state) => {
      const existingIdx = state.notifications.findIndex((n) => n.id === id)
      if (existingIdx >= 0) {
        // 同 id 重复投递（重连重放 / pending 轮询重入）：更新而非新增——内容
        // 与时间戳以最新投递为准，已读态保持（重放不是新活动，badge 不增长、
        // 不重新弹层）。
        const merged: NotificationItem = {
          ...newItem,
          isRead: state.notifications[existingIdx].isRead,
        }
        const updated = [...state.notifications]
        updated[existingIdx] = merged
        return {
          notifications: sortNotifications(updated),
          ...(state.activeBlockingNotification?.id === id
            ? { activeBlockingNotification: merged }
            : {}),
        }
      }

      // 已被用户移除/清空的通知 id 不再回弹入列
      if (dismissedNotificationIds.has(id)) return state

      const updated = sortNotifications([...state.notifications, newItem])
      const blockingUpdate = checkBlockingNotification(state, newItem)

      // 自动弹层只留给"持久的重要通知"（无 autoDismissMs）：瞬态 toast 类通知
      // （生产方统一 isBlocking:false + autoDismissMs）若也弹全屏抽屉，抽屉会在
      // 通知自动消失后变成空屉继续遮挡整页（GUI 黑盒测试 2026-09-11 实测复现）。
      const shouldAutoOpen =
        !state.isPanelOpen &&
        !newItem.autoDismissMs &&
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
    rememberDismissedId(id)
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
    for (const n of get().notifications) {
      rememberDismissedId(n.id)
    }
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
        // 不带 actionId：confirm 动作按默认确认处理（解除阻塞+已读）。
        // 带 id 重入会构成 confirm↔executeAction 无限互递归（动作必带 id，
        // 用户点击确认按钮即栈溢出），navigate/dismiss/custom 不受影响。
        get().confirmBlockingNotification()
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
