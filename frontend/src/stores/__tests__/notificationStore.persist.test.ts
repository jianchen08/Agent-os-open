// @feature: FP-T12 前端适配(X14-F1/P-134 通知中心持久化) | @ci: frontend-test
/**
 * X14-F1（P-134，P-138 同刀）：通知中心持久化契约
 *
 * 修复前：通知列表与已读态纯内存——刷新全丢、kernel replay 窗口（1000 条/5min）
 * 帧重入列变未读、badge 漂移。本文件锁定持久化契约：
 *
 * - 轻量面持久化（zustand persist + createTolerantStorage/userScopedKey 分户）：
 *   条目摘要（id/title/category/priority/isRead/taskId/sessionId/timestamp，
 *   最近 cap 条）+ 已读 id 集合 + 已移除 id 记忆；正文全文/actions 载荷不落盘；
 *   瞬态 toast（autoDismissMs 且非阻塞）不落盘；
 * - 模拟刷新（新 store 实例 rehydrate）后：条目摘要与已读态存续，badge 不漂移；
 * - 重放帧（同 id）不重复入列、不把已读刷回未读；已读 id 被 cap 淘汰后仍由
 *   持久化已读集合判重；dismissed 记忆跨刷新生效（BUG-74 契约跨刷新延伸）；
 * - 列表 cap=200（P-138 同刀）：超出淘汰最旧 normal/low 优先，critical/high
 *   保留；纯 critical/high 病态洪泛走最旧优先硬上限兜底；cap 淘汰≠用户移除
 *   （被淘汰未读 id 重投可再入列）；
 * - 分户键隔离：不同账户经 userScopedKey 各用各键，读写互不可达。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STORAGE_KEYS } from '@/constants/storage'
import { userScopedKey } from '@/utils/userScopedStorage'
import type { NotificationItem, NotificationPriority } from '@/types/notification'

type StoreApi = {
  persist?: {
    hasHydrated: () => boolean
    onFinishHydration: (cb: () => void) => () => void
  }
}

/** 本文件消费的 store 能力面（结构化声明，避免动态 import 类型注解） */
interface NotificationStoreApi {
  getState: () => {
    notifications: NotificationItem[]
    addNotification: (data: AddInput) => string
    markAsRead: (id: string) => void
    clearAll: () => void
    getUnreadCount: () => number
  }
}

/** 新建 store 实例并等待 persist rehydrate 完成（模拟页面刷新后的启动时序） */
async function freshStore(): Promise<NotificationStoreApi> {
  vi.resetModules()
  const mod = await import('@/stores/notificationStore')
  const store = mod.useNotificationStore
  const persistApi = (store as unknown as StoreApi).persist
  if (persistApi && !persistApi.hasHydrated()) {
    await new Promise<void>((resolve) => {
      const unsub = persistApi.onFinishHydration(() => {
        unsub()
        resolve()
      })
    })
  }
  return store
}

/** 读持久化信封里的 state 面（persist JSON：{ state, version }） */
function persistedState(key: string): {
  notifications?: Array<Record<string, unknown>>
  readIds?: string[]
  dismissedIds?: string[]
} {
  const raw = localStorage.getItem(key)
  expect(raw).not.toBeNull()
  return (JSON.parse(raw as string) as { state: Record<string, unknown> }).state as {
    notifications?: Array<Record<string, unknown>>
    readIds?: string[]
    dismissedIds?: string[]
  }
}

function anonKey(): string {
  return userScopedKey('notification-storage')
}

type AddInput = Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'> & { id?: string }

function makeInput(overrides: Partial<AddInput> = {}): AddInput {
  return {
    title: '通知',
    priority: 'normal',
    category: 'info',
    isBlocking: false,
    sourceLabel: '测试',
    ...overrides,
  }
}

const pad = (n: number): string => String(n).padStart(3, '0')

beforeEach(() => {
  localStorage.clear()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('X14-F1 刷新存续（轻量面持久化）', () => {
  it('刷新后条目摘要与已读态存续；正文/actions 不落盘；瞬态 toast 不落盘', async () => {
    const store = await freshStore()

    vi.setSystemTime(1_000)
    const readId = store.getState().addNotification(
      makeInput({
        id: 'ntf-read',
        title: '已读通知',
        message: `很长的正文。`.repeat(200),
        priority: 'normal',
        category: 'info',
        actions: [{ id: 'a1', label: '查看', action: 'navigate' }],
        taskId: 'task-9',
      }),
    )
    store.getState().markAsRead(readId)
    vi.setSystemTime(2_000)
    store.getState().addNotification(
      makeInput({ id: 'ntf-unread', title: '未读通知', priority: 'high', category: 'alert', sessionId: 'sess-7' }),
    )
    vi.setSystemTime(3_000)
    store.getState().addNotification(
      makeInput({ id: 'ntf-toast', title: '瞬态提示', priority: 'high', category: 'error', autoDismissMs: 6000 }),
    )

    // 落盘面：只存轻量摘要
    const persisted = persistedState(anonKey())
    const entries = persisted.notifications ?? []
    const entryIds = entries.map((e) => e.id)
    expect(entryIds).toContain('ntf-read')
    expect(entryIds).toContain('ntf-unread')
    expect(entryIds).not.toContain('ntf-toast')
    const readEntry = entries.find((e) => e.id === 'ntf-read') as Record<string, unknown>
    expect(readEntry.isRead).toBe(true)
    expect(readEntry.taskId).toBe('task-9')
    expect(readEntry.title).toBe('已读通知')
    expect('message' in readEntry).toBe(false)
    expect('actions' in readEntry).toBe(false)

    // 模拟刷新：全新 store 实例 rehydrate
    const reloaded = await freshStore()
    const list = reloaded.getState().notifications
    expect(list.map((n) => n.id).sort()).toEqual(['ntf-read', 'ntf-unread'])
    const reloadedRead = list.find((n) => n.id === 'ntf-read') as NotificationItem
    expect(reloadedRead.isRead).toBe(true)
    expect(reloadedRead.taskId).toBe('task-9')
    expect(reloadedRead.message).toBeUndefined()
    // badge 按存续已读态计数：只有未读通知计 1，不因刷新漂移
    expect(reloaded.getState().getUnreadCount()).toBe(1)
  })
})

describe('X14-F1 重放窗口帧判重', () => {
  it('刷新后重放帧（同 id）不重复入列不变未读', async () => {
    const store = await freshStore()
    vi.setSystemTime(1_000)
    const id = store.getState().addNotification(
      makeInput({ id: 'replay-1', title: 'A', priority: 'high', category: 'alert' }),
    )
    store.getState().markAsRead(id)

    const reloaded = await freshStore()
    expect(reloaded.getState().notifications.map((n) => n.id)).toEqual(['replay-1'])
    // kernel replay 重投同 id（重放不是新活动）
    reloaded.getState().addNotification(
      makeInput({ id: 'replay-1', title: 'A', priority: 'high', category: 'alert' }),
    )
    const list = reloaded.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].isRead).toBe(true)
    expect(reloaded.getState().getUnreadCount()).toBe(0)
  })

  it('已读 id 被列表 cap 淘汰后，重放帧按持久化已读集合判重不入列', async () => {
    // 播种持久化面：已读集合含 id、条目列表已无该 id（模拟 cap 淘汰后的存量）
    localStorage.setItem(
      anonKey(),
      JSON.stringify({ state: { notifications: [], readIds: ['evicted-read'], dismissedIds: [] }, version: 1 }),
    )
    const reloaded = await freshStore()
    reloaded.getState().addNotification(
      makeInput({ id: 'evicted-read', title: '重放', priority: 'normal', category: 'info' }),
    )
    expect(reloaded.getState().notifications).toHaveLength(0)
    expect(reloaded.getState().getUnreadCount()).toBe(0)
  })

  it('刷新后已清空通知的重放帧不回弹（dismissed 记忆持久化）', async () => {
    const store = await freshStore()
    vi.setSystemTime(1_000)
    store.getState().addNotification(makeInput({ id: 'cleared-1', title: 'X' }))
    store.getState().clearAll()

    const persisted = persistedState(anonKey())
    expect(persisted.dismissedIds).toContain('cleared-1')

    const reloaded = await freshStore()
    reloaded.getState().addNotification(makeInput({ id: 'cleared-1', title: 'X' }))
    expect(reloaded.getState().notifications).toHaveLength(0)
  })
})

describe('X14-F1 列表 cap（P-138 同刀收口）', () => {
  it('超过 cap=200 淘汰最旧 normal/low 优先，critical/high 保留', async () => {
    const store = await freshStore()
    const add = (id: string, priority: NotificationPriority) => {
      store.getState().addNotification(makeInput({ id, title: id, priority, category: 'info' }))
    }
    for (let i = 0; i < 150; i++) {
      vi.setSystemTime(1_000 + i)
      add(`n-${pad(i)}`, 'normal')
    }
    for (let i = 0; i < 50; i++) {
      vi.setSystemTime(1_500 + i)
      add(`c-${pad(i)}`, 'critical')
    }
    expect(store.getState().notifications).toHaveLength(200)

    // 再入 60 条 normal：总数 260 → 淘汰 60 条最旧 normal（n-000..n-059）
    for (let i = 150; i < 210; i++) {
      vi.setSystemTime(3_000 + i)
      add(`n-${pad(i)}`, 'normal')
    }
    const list = store.getState().notifications
    expect(list).toHaveLength(200)
    const ids = new Set(list.map((n) => n.id))
    expect(ids.has('n-000')).toBe(false)
    expect(ids.has('n-059')).toBe(false)
    expect(ids.has('n-060')).toBe(true)
    expect(ids.has('n-209')).toBe(true)
    for (let i = 0; i < 50; i++) {
      expect(ids.has(`c-${pad(i)}`)).toBe(true)
    }
  })

  it('纯 critical/high 洪泛仍超硬上限：最旧优先兜底淘汰（有界是 cap 本义）', async () => {
    const store = await freshStore()
    for (let i = 0; i < 205; i++) {
      vi.setSystemTime(1_000 + i)
      store.getState().addNotification(makeInput({ id: `h-${pad(i)}`, title: `h-${pad(i)}`, priority: 'high', category: 'alert' }))
    }
    const list = store.getState().notifications
    expect(list).toHaveLength(200)
    const ids = new Set(list.map((n) => n.id))
    expect(ids.has('h-000')).toBe(false)
    expect(ids.has('h-004')).toBe(false)
    expect(ids.has('h-005')).toBe(true)
    expect(ids.has('h-204')).toBe(true)
  })

  it('cap 淘汰不是用户移除：被淘汰的未读 id 重投可再入列', async () => {
    const store = await freshStore()
    for (let i = 0; i < 205; i++) {
      vi.setSystemTime(1_000 + i)
      store.getState().addNotification(makeInput({ id: `n-${pad(i)}`, title: `n-${pad(i)}` }))
    }
    expect(new Set(store.getState().notifications.map((n) => n.id)).has('n-000')).toBe(false)

    // 重投被淘汰 id：非 dismissed/read 记忆命中，照常入列（列表管理≠用户裁决）
    vi.setSystemTime(9_000)
    store.getState().addNotification(makeInput({ id: 'n-000', title: 'n-000' }))
    expect(new Set(store.getState().notifications.map((n) => n.id)).has('n-000')).toBe(true)
  })

  it('持久化面同样有界：列表条目摘要落盘不超过 200 条', async () => {
    const store = await freshStore()
    for (let i = 0; i < 205; i++) {
      vi.setSystemTime(1_000 + i)
      store.getState().addNotification(makeInput({ id: `p-${pad(i)}`, title: `p-${pad(i)}` }))
    }
    const entries = persistedState(anonKey()).notifications ?? []
    expect(entries).toHaveLength(200)
  })
})

describe('X14-F1 分户键隔离', () => {
  it('不同账户经 userScopedKey 各用各键，读写互不可见', async () => {
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify({ id: 'user-a' }))
    const storeA = await freshStore()
    vi.setSystemTime(1_000)
    storeA.getState().addNotification(makeInput({ id: 'a-only', title: 'A 的通知' }))
    expect(localStorage.getItem('ag-u:user-a:notification-storage')).not.toBeNull()

    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify({ id: 'user-b' }))
    const storeB = await freshStore()
    // user-a 的持久化通知对 user-b 不可见（badge 互不可达）
    expect(storeB.getState().notifications).toHaveLength(0)
    vi.setSystemTime(2_000)
    storeB.getState().addNotification(makeInput({ id: 'b-only', title: 'B 的通知' }))
    const bPersisted = persistedState('ag-u:user-b:notification-storage')
    expect((bPersisted.notifications ?? []).map((e) => e.id)).toEqual(['b-only'])
    // user-a 键未被 user-b 写穿
    const aPersisted = persistedState('ag-u:user-a:notification-storage')
    expect((aPersisted.notifications ?? []).map((e) => e.id)).toEqual(['a-only'])
  })
})
