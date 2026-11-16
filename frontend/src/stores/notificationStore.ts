/**
 * 通知状态管理 Store
 *
 * 管理非阻塞/阻塞通知队列（时间倒序，最新在最前）。
 * 纯状态层，不涉及通信或 UI。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import type {
  NotificationItem,
  NotificationCategory,
  NotificationPriority,
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
 * 重复投递的事件可能不携带稳定 id（纯前端错误提示等），按正文指纹兜底
 * （标题不参与：同一失败经不同上报层可能带不同标题）。
 */
const recentFingerprints = new Map<string, { id: string; at: number }>()

/**
 * 指纹去重窗口：必须覆盖重复投递的实际间隔抖动（/interaction/pending 兜底
 * 轮询周期 10s + 网络延迟），10s 整会因投递抖动漏挡，故取 30s。
 */
const FINGERPRINT_DEDUP_WINDOW_MS = 30_000

/** 已被用户移除/清空的通知 id 记忆上限（长期会话防无界增长） */
const DISMISSED_ID_CAP = 1000

/** 已读 id 记忆上限：覆盖 kernel replay 窗口（1000 条/5min）的重放判重面 */
const READ_ID_CAP = 1000

/** 通知列表条目上限（P-138 同刀收口）：超出淘汰最旧 normal/low 优先 */
const NOTIFICATION_LIST_CAP = 200

/** FIFO 收拢 id 集合到上限（Set 插入序=首入序：重复 add 不改变既有位次） */
function capIdSet(ids: Set<string>, cap: number): Set<string> {
  while (ids.size > cap) {
    const oldest = ids.values().next().value
    if (oldest === undefined) break
    ids.delete(oldest)
  }
  return ids
}

/** 记忆集合新增 id（越界淘汰最旧；已存在不改位次） */
function addIdCapped(ids: Set<string>, id: string, cap: number): Set<string> {
  if (ids.has(id)) return ids
  const next = new Set(ids)
  next.add(id)
  return capIdSet(next, cap)
}

/** 从持久化数组重建 id 集合（畸形档按空处理，越界 FIFO 收拢） */
function idSetFrom(value: unknown, cap: number): Set<string> {
  const out = new Set<string>()
  if (!Array.isArray(value)) return out
  for (const v of value) {
    if (typeof v !== 'string') continue
    out.add(v)
  }
  return capIdSet(out, cap)
}

/** 持久化条目摘要（轻量面）：正文/progress/actions/autoDismissMs 等全文面不落盘 */
interface PersistedNotificationEntry {
  id: string
  title: string
  category: NotificationCategory
  priority: NotificationPriority
  isRead: boolean
  timestamp: string
  taskId?: string
  sessionId?: string
}

/** notification-storage 持久化 state 形状（partialize 输出 / merge 输入） */
interface PersistedNotificationState {
  notifications: PersistedNotificationEntry[]
  readIds: string[]
  dismissedIds: string[]
}

/** 瞬态 toast（autoDismissMs 且非阻塞，与自动消失计时条件同款）不持久化 */
function isTransientToast(n: NotificationItem): boolean {
  return !!n.autoDismissMs && !n.isBlocking
}

/** 条目 → 持久化摘要：路由坐标保留 task/session，其余全文面丢弃 */
function toPersistedEntry(n: NotificationItem): PersistedNotificationEntry {
  return {
    id: n.id,
    title: n.title,
    category: n.category,
    priority: n.priority,
    isRead: n.isRead,
    timestamp: n.timestamp,
    ...(n.taskId ? { taskId: n.taskId } : {}),
    ...(n.sessionId ? { sessionId: n.sessionId } : {}),
  }
}

/** 持久化条目序列：滤瞬态 + 按时间取最近 cap 条（有界落盘） */
function pickPersistableEntries(items: NotificationItem[]): PersistedNotificationEntry[] {
  return items
    .filter((n) => !isTransientToast(n))
    .sort((a, b) => new Date(b.timestamp).getTime() - new Date(a.timestamp).getTime())
    .slice(0, NOTIFICATION_LIST_CAP)
    .map(toPersistedEntry)
}

/** 持久化摘要 → 列表条目（全文面缺省即不渲染；恢复条目按非阻塞态入列） */
function revivePersistedEntry(e: PersistedNotificationEntry): NotificationItem {
  return { ...e, isBlocking: false }
}

/** 持久化摘要形状守卫（外力改写的畸形档按条目级丢弃，不抛错） */
function isPersistedEntryShape(v: unknown): v is PersistedNotificationEntry {
  if (typeof v !== 'object' || v === null) return false
  const e = v as Record<string, unknown>
  return (
    typeof e.id === 'string' &&
    typeof e.title === 'string' &&
    typeof e.category === 'string' &&
    (e.priority === 'critical' || e.priority === 'high' || e.priority === 'normal' || e.priority === 'low') &&
    typeof e.isRead === 'boolean' &&
    typeof e.timestamp === 'string'
  )
}

const notificationTimestamp = (n: NotificationItem): number => new Date(n.timestamp).getTime()

/**
 * 列表上限治理（P-138 同刀收口）：超出 cap 先淘汰最旧 normal/low；全部
 * critical/high 仍超限（病态洪泛）再按最旧优先硬淘汰——有界是 cap 本义。
 * 淘汰是列表管理动作，不进 dismissed/read 记忆（重投可再入列）。
 */
function evictOverCap(items: NotificationItem[]): {
  notifications: NotificationItem[]
  evictedIds: string[]
} {
  if (items.length <= NOTIFICATION_LIST_CAP) return { notifications: items, evictedIds: [] }

  const dropped = new Set<string>()
  let over = items.length - NOTIFICATION_LIST_CAP
  const softCandidates = items
    .filter((n) => n.priority === 'normal' || n.priority === 'low')
    .sort((a, b) => notificationTimestamp(a) - notificationTimestamp(b))
  for (const n of softCandidates) {
    if (over <= 0) break
    dropped.add(n.id)
    over -= 1
  }
  if (over > 0) {
    const hardCandidates = items
      .filter((n) => !dropped.has(n.id))
      .sort((a, b) => notificationTimestamp(a) - notificationTimestamp(b))
    for (const n of hardCandidates) {
      if (over <= 0) break
      dropped.add(n.id)
      over -= 1
    }
  }
  return {
    notifications: items.filter((n) => !dropped.has(n.id)),
    evictedIds: [...dropped],
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
  /** 通知列表（时间倒序，最新在最前） */
  notifications: NotificationItem[]
  /** 通知中心面板是否展开 */
  isPanelOpen: boolean
  /** 当前阻塞式通知（仅一条，模态展示） */
  activeBlockingNotification: NotificationItem | null
  /** 已读 id 记忆（持久化判重面）：条目被列表 cap 淘汰后重放帧仍按此判重不回弹未读 */
  readIds: Set<string>
  /** 已被用户移除/清空的通知 id 记忆（持久化）：同 id 重投不回弹 */
  dismissedIds: Set<string>

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
  /** 按目标坐标（taskId/sessionId）摘除关联通知（删任务/删会话时调用，X18-F1） */
  pruneForTarget: (target: { taskId?: string; sessionId?: string }) => void
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
  /** 切换面板展开/收起 */
  togglePanel: () => void
  /** 打开面板 */
  openPanel: () => void
  /** 关闭面板 */
  closePanel: () => void
  /** 获取未读计数 */
  getUnreadCount: () => number
}

/**
 * 排序通知：时间倒序（最新在最前；同毫秒保持入队顺序，稳定排序）
 */
export function sortNotificationsNewestFirst(items: NotificationItem[]): NotificationItem[] {
  return [...items].sort((a, b) => notificationTimestamp(b) - notificationTimestamp(a))
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

/**
 * rehydrate 合并（X14-F1）：持久化摘要与水合前已到达的条目取并集（水合是
 * 异步微任务链，早到帧不被覆盖）；同 id 已读态胜出（重放不回弹为未读）。
 * 畸形档按条目级丢弃 / 集合按空处理（fail-closed 回默认态，不抛错）。
 */
function mergePersistedState(
  persisted: unknown,
  current: NotificationState,
): NotificationState {
  const p = (
    typeof persisted === 'object' && persisted !== null ? persisted : {}
  ) as Partial<PersistedNotificationState>
  const restoredEntries = Array.isArray(p.notifications) ? p.notifications : []
  const byId = new Map<string, NotificationItem>()
  for (const entry of restoredEntries) {
    if (isPersistedEntryShape(entry)) byId.set(entry.id, revivePersistedEntry(entry))
  }
  for (const item of current.notifications) {
    const prev = byId.get(item.id)
    if (prev?.isRead && !item.isRead) continue
    byId.set(item.id, item)
  }
  const merged = evictOverCap(sortNotificationsNewestFirst([...byId.values()]))
  const readIds = idSetFrom(p.readIds, READ_ID_CAP)
  for (const id of current.readIds) readIds.add(id)
  const dismissedIds = idSetFrom(p.dismissedIds, DISMISSED_ID_CAP)
  for (const id of current.dismissedIds) dismissedIds.add(id)
  return {
    ...current,
    notifications: merged.notifications,
    readIds: capIdSet(readIds, READ_ID_CAP),
    dismissedIds: capIdSet(dismissedIds, DISMISSED_ID_CAP),
  }
}

export const useNotificationStore = create<NotificationState>()(
  persist(
    (set, get) => ({
  notifications: [],
  isPanelOpen: false,
  activeBlockingNotification: null,
  readIds: new Set<string>(),
  dismissedIds: new Set<string>(),

  addNotification: (data) => {
    const now = new Date()
    const id = data.id ?? generateNotificationId()

    // 无 id 通知兜底闸：同正文短窗内只入一条（重连重放/轮询重入/同一失败
    // 多层上报等重复投递不携带稳定 id 时按内容指纹挡住刷屏）。命中时返回首次
    // 入列 id。标题不参与指纹：同一次失败可能经不同上报层带不同标题（如 HTTP
    // 拦截器「服务请求失败」+ 调用方「操作失败」），标题参与会让同一失败漏挡
    // 成两条。无正文时退回标题作键。
    // 携带关联坐标（sessionId/sourceId/taskId/agentId）的通知不走此闸：这类
    // 是按实体语义的重复事件（如逐 pipeline 的命中率骤降告警），短窗吞掉
    // 会让真实告警丢失——它们的重复投递应在生产方修复。
    if (!data.id && !data.sessionId && !data.sourceId && !data.taskId && !data.agentId) {
      const fingerprint = data.message?.trim() ? data.message : data.title
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
          notifications: sortNotificationsNewestFirst(updated),
          ...(state.activeBlockingNotification?.id === id
            ? { activeBlockingNotification: merged }
            : {}),
        }
      }

      // 已被用户移除/清空的通知 id 不再回弹入列
      if (state.dismissedIds.has(id)) return state
      // 已读 id 不因重放回弹为未读（判重面含被列表 cap 淘汰的存量：badge 不漂移）
      if (state.readIds.has(id)) return state

      const added = sortNotificationsNewestFirst([...state.notifications, newItem])
      const { notifications: capped, evictedIds } = evictOverCap(added)
      const blockingUpdate = checkBlockingNotification(state, newItem)
      const activeEvicted = state.activeBlockingNotification
        ? evictedIds.includes(state.activeBlockingNotification.id)
        : false

      // 自动弹层只留给"持久的重要通知"（无 autoDismissMs）：瞬态 toast 类通知
      // （生产方统一 isBlocking:false + autoDismissMs）若也弹全屏抽屉，抽屉会在
      // 通知自动消失后变成空屉继续遮挡整页（GUI 黑盒测试 2026-09-11 实测复现）。
      const shouldAutoOpen =
        !state.isPanelOpen &&
        !newItem.autoDismissMs &&
        (newItem.priority === 'high' || newItem.priority === 'critical')

      return {
        notifications: capped,
        ...blockingUpdate,
        ...(activeEvicted ? { activeBlockingNotification: null } : {}),
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
      const { notifications: capped, evictedIds } = evictOverCap(
        sortNotificationsNewestFirst([...state.notifications, ...newItems]),
      )
      const firstBlocking = newItems.find(
        (n) => n.isBlocking && !evictedIds.includes(n.id),
      )
      const blockingUpdate =
        firstBlocking && !state.activeBlockingNotification
          ? { activeBlockingNotification: firstBlocking }
          : {}
      const activeEvicted = state.activeBlockingNotification
        ? evictedIds.includes(state.activeBlockingNotification.id)
        : false

      return {
        notifications: capped,
        ...blockingUpdate,
        ...(activeEvicted ? { activeBlockingNotification: null } : {}),
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
        dismissedIds: addIdCapped(state.dismissedIds, id, DISMISSED_ID_CAP),
        ...blockingUpdate,
      }
    })
  },

  markAsRead: (id) => {
    set((state) => ({
      notifications: state.notifications.map((n) =>
        n.id === id ? { ...n, isRead: true } : n,
      ),
      readIds: addIdCapped(state.readIds, id, READ_ID_CAP),
    }))
  },

  markAllAsRead: () => {
    set((state) => {
      let readIds = state.readIds
      for (const n of state.notifications) {
        readIds = addIdCapped(readIds, n.id, READ_ID_CAP)
      }
      return {
        notifications: state.notifications.map((n) => ({ ...n, isRead: true })),
        readIds,
      }
    })
  },

  clearAll: () => {
    set((state) => {
      let dismissedIds = state.dismissedIds
      for (const n of state.notifications) {
        dismissedIds = addIdCapped(dismissedIds, n.id, DISMISSED_ID_CAP)
      }
      return {
        notifications: [],
        dismissedIds,
        activeBlockingNotification: null,
      }
    })
  },

  // X18-F1（P-139）：删任务/删会话时摘除带该坐标的条目——残留条目只能点击
  // 空路由留驻到刷新。摘除即 dismissed 记忆（与用户移除/清空同语义：同 id
  // 重投不回魂，persist 面随 dismissedIds/条目摘要落盘同步）。带该坐标的
  // 阻塞模态一并解除（其点击/确认都会路由到已不存在的目标）。
  pruneForTarget: (target) => {
    const taskId = target.taskId
    const sessionId = target.sessionId
    if (!taskId && !sessionId) return
    set((state) => {
      const prunedIds: string[] = []
      const kept = state.notifications.filter((n) => {
        const hit =
          (taskId !== undefined && n.taskId === taskId) ||
          (sessionId !== undefined && n.sessionId === sessionId)
        if (hit) prunedIds.push(n.id)
        return !hit
      })
      if (prunedIds.length === 0) return state
      let dismissedIds = state.dismissedIds
      for (const id of prunedIds) {
        dismissedIds = addIdCapped(dismissedIds, id, DISMISSED_ID_CAP)
      }
      return {
        notifications: kept,
        dismissedIds,
        ...(state.activeBlockingNotification &&
        prunedIds.includes(state.activeBlockingNotification.id)
          ? { activeBlockingNotification: null }
          : {}),
      }
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
      readIds: addIdCapped(state.readIds, activeBlockingNotification.id, READ_ID_CAP),
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
    }),
    {
      name: 'notification-storage',
      version: 1,
      // 配额满时吞掉 QuotaExceededError，避免 addNotification 等 action 崩溃
      storage: createTolerantStorage(),
      // 只存轻量面（X14-F1）：条目摘要（最近 cap 条，正文/载荷不落盘）+
      // 已读/已移除 id 记忆；瞬态 toast、面板开关、折叠态与阻塞模态不落盘
      partialize: (state): PersistedNotificationState => ({
        notifications: pickPersistableEntries(state.notifications),
        readIds: [...state.readIds],
        dismissedIds: [...state.dismissedIds],
      }),
      merge: (persisted, current) => mergePersistedState(persisted, current),
    },
  ),
)

/**
 * 清空已移除/已读通知 id 记忆（测试隔离用）：两集合随 persist 落盘，跨测试
 * 用例会泄漏挡住同 id 重报；生产链路无调用方。
 */
export function resetDismissedNotificationIdsForTest(): void {
  useNotificationStore.setState({ dismissedIds: new Set(), readIds: new Set() })
}
