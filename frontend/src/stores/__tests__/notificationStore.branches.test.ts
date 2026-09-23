/** @feature FP-T12 前端组件补测 | @ci frontend-test */
/**
 * notificationStore 分支补测：
 * - 入队：优先级降序 + 同优先级时间升序排序、显式 id 去重、批量添加
 * - 阻塞通知：activeBlockingNotification 的设置/不顶替/移除联动、升级
 * - 已读/移除/清空/进度钳制
 * - confirmBlockingNotification 动作分流（命中 navigate / 未命中回落默认）
 * - executeAction dismiss/navigate/custom 分支
 * - 折叠/面板开关、未读计数与按优先级过滤
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NotificationItem, NotificationPriority } from '@/types/notification'

let store: typeof import('@/stores/notificationStore').useNotificationStore

beforeEach(async () => {
  vi.resetModules()
  const mod = await import('@/stores/notificationStore')
  store = mod.useNotificationStore
})

afterEach(() => {
  vi.useRealTimers()
})

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

/** 在指定系统时间入队（timestamp 由 store 取当前时间，排序断言需要可区分的时间） */
function addAt(epochMs: number, data: AddInput): string {
  vi.setSystemTime(epochMs)
  return store.getState().addNotification(data)
}

describe('notificationStore 入队与排序', () => {
  it('跨优先级降序排序：后入队的 critical 排在前', () => {
    addAt(1_000, makeInput({ title: '低优', priority: 'low' }))
    addAt(2_000, makeInput({ title: '危急', priority: 'critical' }))
    const priorities = store.getState().notifications.map((n) => n.priority)
    expect(priorities).toEqual(['critical', 'low'])
  })

  it('同优先级按时间升序（先到先排，非按插入位置）', () => {
    addAt(2_000, makeInput({ title: '先入队' }))
    addAt(1_000, makeInput({ title: '后入队但时间更早' }))
    const titles = store.getState().notifications.map((n) => n.title)
    expect(titles).toEqual(['后入队但时间更早', '先入队'])
  })

  it('显式 id 重复入队更新而非新增：最新内容生效、只保留一条', () => {
    store.getState().addNotification(makeInput({ id: 'dup-1', title: '第一次' }))
    store.getState().addNotification(makeInput({ id: 'dup-1', title: '第二次' }))
    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].title).toBe('第二次')
    expect(list[0].isRead).toBe(false)
  })

  it('addNotifications 批量生成唯一 id、置未读并整体排序', () => {
    vi.useFakeTimers()
    vi.setSystemTime(5_000)
    const ids = store.getState().addNotifications([
      makeInput({ title: '普通', priority: 'normal' }),
      makeInput({ title: '高优', priority: 'high' }),
    ])
    expect(ids).toHaveLength(2)
    expect(new Set(ids).size).toBe(2)
    const list = store.getState().notifications
    expect(list.map((n) => n.priority)).toEqual(['high', 'normal'])
    expect(list.every((n) => n.isRead === false)).toBe(true)
  })

  it('批量携带阻塞通知时首个设为 activeBlockingNotification', () => {
    store.getState().addNotifications([
      makeInput({ title: '非阻塞' }),
      makeInput({ title: '阻塞', isBlocking: true }),
      makeInput({ title: '阻塞二', isBlocking: true }),
    ])
    expect(store.getState().activeBlockingNotification?.title).toBe('阻塞')
  })
})

describe('notificationStore 阻塞通知生命周期', () => {
  it('阻塞通知入队即设为 activeBlockingNotification；已有阻塞时不顶替', () => {
    addAt(1_000, makeInput({ id: 'b-1', title: '阻塞一', isBlocking: true }))
    expect(store.getState().activeBlockingNotification?.id).toBe('b-1')
    addAt(2_000, makeInput({ id: 'b-2', title: '阻塞二', isBlocking: true }))
    expect(store.getState().activeBlockingNotification?.id).toBe('b-1')
  })

  it('非阻塞通知不设置 activeBlockingNotification', () => {
    store.getState().addNotification(makeInput({ title: '普通' }))
    expect(store.getState().activeBlockingNotification).toBeNull()
  })

  it('escalateToBlocking：升级为 high 阻塞并在无阻塞时占位', () => {
    const id = store.getState().addNotification(makeInput({ id: 'e-1', priority: 'low' }))
    store.getState().escalateToBlocking(id)
    const escalated = store.getState().notifications.find((n) => n.id === id)
    expect(escalated?.isBlocking).toBe(true)
    expect(escalated?.priority).toBe('high')
    expect(store.getState().activeBlockingNotification?.id).toBe(id)
  })

  it('escalateToBlocking：已有阻塞通知时不顶替', () => {
    addAt(1_000, makeInput({ id: 'b-1', isBlocking: true }))
    const id = store.getState().addNotification(makeInput({ id: 'e-2', priority: 'low' }))
    store.getState().escalateToBlocking(id)
    expect(store.getState().activeBlockingNotification?.id).toBe('b-1')
    expect(store.getState().notifications.find((n) => n.id === id)?.isBlocking).toBe(true)
  })

  it('escalateToBlocking：目标不存在时状态不变', () => {
    store.getState().escalateToBlocking('ghost')
    expect(store.getState().notifications).toHaveLength(0)
    expect(store.getState().activeBlockingNotification).toBeNull()
  })

  it('removeNotification：移除阻塞通知联动清空 activeBlockingNotification', () => {
    const id = store.getState().addNotification(makeInput({ isBlocking: true }))
    store.getState().removeNotification(id)
    expect(store.getState().activeBlockingNotification).toBeNull()
    expect(store.getState().notifications).toHaveLength(0)
  })

  it('removeNotification：移除非阻塞通知不影响 activeBlockingNotification', () => {
    addAt(1_000, makeInput({ id: 'b-1', isBlocking: true }))
    const other = store.getState().addNotification(makeInput({ id: 'n-1' }))
    store.getState().removeNotification(other)
    expect(store.getState().activeBlockingNotification?.id).toBe('b-1')
  })

  it('removeNotification：不存在的 id 为无副作用空操作', () => {
    store.getState().addNotification(makeInput({ id: 'keep-1' }))
    store.getState().removeNotification('ghost')
    expect(store.getState().notifications).toHaveLength(1)
  })

  it('dismissNotification 与 removeNotification 同语义', () => {
    const id = store.getState().addNotification(makeInput({ title: '待忽略' }))
    store.getState().dismissNotification(id)
    expect(store.getState().notifications).toHaveLength(0)
  })
})

describe('notificationStore 已读与清空', () => {
  it('markAsRead 只标记目标，未读计数随之下降', () => {
    const a = addAt(1_000, makeInput({ id: 'a', priority: 'high' }))
    addAt(2_000, makeInput({ id: 'b', priority: 'normal' }))
    expect(store.getState().getUnreadCount()).toBe(2)
    store.getState().markAsRead(a)
    expect(store.getState().getUnreadCount()).toBe(1)
    expect(store.getState().getUnreadCountByPriority('high')).toBe(0)
    expect(store.getState().getUnreadCountByPriority('normal')).toBe(1)
  })

  it('markAllAsRead 清零全部未读', () => {
    addAt(1_000, makeInput({ priority: 'high' }))
    addAt(2_000, makeInput({ priority: 'low' }))
    store.getState().markAllAsRead()
    expect(store.getState().getUnreadCount()).toBe(0)
  })

  it('clearAll 同时清空列表与阻塞通知', () => {
    store.getState().addNotification(makeInput({ isBlocking: true }))
    store.getState().openPanel()
    store.getState().clearAll()
    const s = store.getState()
    expect(s.notifications).toHaveLength(0)
    expect(s.activeBlockingNotification).toBeNull()
  })
})

describe('notificationStore 进度更新钳制', () => {
  it.each([
    ['负值钳到 0', -10, 0],
    ['区间内原样', 42, 42],
    ['超 100 钳到 100', 150, 100],
  ])('updateProgress：%s（%d → %d）', (_label, input, expected) => {
    const id = store.getState().addNotification(
      makeInput({ id: 'p-1', category: 'progress', priority: 'normal' }),
    )
    store.getState().updateProgress(id, input)
    expect(store.getState().notifications.find((n) => n.id === id)?.progress).toBe(expected)
  })

  it('updateProgress 同步当前阻塞通知的进度；非目标不串写', () => {
    addAt(1_000, makeInput({ id: 'b-1', isBlocking: true, category: 'progress' }))
    const other = addAt(2_000, makeInput({ id: 'p-2', category: 'progress' }))
    store.getState().updateProgress(other, 66)
    expect(store.getState().activeBlockingNotification?.progress).toBeUndefined()
    store.getState().updateProgress('b-1', 77)
    expect(store.getState().activeBlockingNotification?.progress).toBe(77)
  })
})

describe('notificationStore 确认与动作分流', () => {
  it('confirmBlockingNotification 无阻塞通知时为空操作', () => {
    store.getState().confirmBlockingNotification()
    expect(store.getState().notifications).toHaveLength(0)
  })

  it('默认确认：标记已读、解除阻塞，但不从列表移除', () => {
    const id = store.getState().addNotification(makeInput({ id: 'b-1', isBlocking: true }))
    store.getState().confirmBlockingNotification()
    const target = store.getState().notifications.find((n) => n.id === id)
    expect(target?.isRead).toBe(true)
    expect(target?.isBlocking).toBe(false)
    expect(store.getState().activeBlockingNotification).toBeNull()
    expect(store.getState().notifications).toHaveLength(1)
  })

  it('actionId 命中 navigate 动作：仅标记已读，阻塞通知保持', () => {
    const id = store.getState().addNotification(
      makeInput({
        id: 'b-1',
        isBlocking: true,
        actions: [{ id: 'go', label: '查看', action: 'navigate' }],
      }),
    )
    store.getState().confirmBlockingNotification('go')
    const target = store.getState().notifications.find((n) => n.id === id)
    expect(target?.isRead).toBe(true)
    expect(store.getState().activeBlockingNotification?.id).toBe(id)
  })

  it('actionId 未命中任何动作：回落默认确认行为', () => {
    const id = store.getState().addNotification(
      makeInput({
        id: 'b-1',
        isBlocking: true,
        actions: [{ id: 'go', label: '查看', action: 'navigate' }],
      }),
    )
    store.getState().confirmBlockingNotification('no-such-action')
    expect(store.getState().notifications.find((n) => n.id === id)?.isRead).toBe(true)
    expect(store.getState().activeBlockingNotification).toBeNull()
  })

  it('executeAction dismiss：移除通知并联动清空阻塞位', () => {
    addAt(1_000, makeInput({ id: 'b-1', isBlocking: true }))
    store.getState().executeAction('b-1', { id: 'x', label: '忽略', action: 'dismiss' })
    expect(store.getState().notifications).toHaveLength(0)
    expect(store.getState().activeBlockingNotification).toBeNull()
  })

  it.each(['navigate', 'custom'] as const)('executeAction %s：只标记已读不移除', (action) => {
    const id = store.getState().addNotification(makeInput({ id: 'n-1' }))
    store.getState().executeAction(id, { id: 'x', label: '按钮', action })
    expect(store.getState().notifications.find((n) => n.id === id)?.isRead).toBe(true)
    expect(store.getState().notifications).toHaveLength(1)
  })
})

describe('notificationStore 面板与折叠', () => {
  it.each(['critical', 'high', 'normal', 'low'] as const)(
    'toggleGroupCollapsed 翻转 %s 折叠态',
    (priority: NotificationPriority) => {
      const before = store.getState().groupState.collapsed[priority]
      store.getState().toggleGroupCollapsed(priority)
      expect(store.getState().groupState.collapsed[priority]).toBe(!before)
      store.getState().toggleGroupCollapsed(priority)
      expect(store.getState().groupState.collapsed[priority]).toBe(before)
    },
  )

  it('togglePanel 来回切换；openPanel/closePanel 显式置位', () => {
    expect(store.getState().isPanelOpen).toBe(false)
    store.getState().togglePanel()
    expect(store.getState().isPanelOpen).toBe(true)
    store.getState().togglePanel()
    expect(store.getState().isPanelOpen).toBe(false)
    store.getState().openPanel()
    expect(store.getState().isPanelOpen).toBe(true)
    store.getState().closePanel()
    expect(store.getState().isPanelOpen).toBe(false)
  })
})

describe('notificationStore 计数与过滤', () => {
  it('getByPriority 只返回该优先级通知', () => {
    addAt(1_000, makeInput({ id: 'h-1', priority: 'high' }))
    addAt(2_000, makeInput({ id: 'h-2', priority: 'high' }))
    addAt(3_000, makeInput({ id: 'l-1', priority: 'low' }))
    const highs = store.getState().getByPriority('high')
    expect(highs.map((n) => n.id)).toEqual(['h-1', 'h-2'])
    expect(store.getState().getByPriority('critical')).toEqual([])
  })

  it('getUnreadCountByPriority 区分已读与未读', () => {
    const h1 = addAt(1_000, makeInput({ id: 'h-1', priority: 'high' }))
    addAt(2_000, makeInput({ id: 'h-2', priority: 'high' }))
    addAt(3_000, makeInput({ id: 'n-1', priority: 'normal' }))
    store.getState().markAsRead(h1)
    expect(store.getState().getUnreadCountByPriority('high')).toBe(1)
    expect(store.getState().getUnreadCountByPriority('normal')).toBe(1)
    expect(store.getState().getUnreadCount()).toBe(2)
  })
})

describe('executeAction - confirm 动作', () => {
  it('action.action=confirm → 走 confirmBlockingNotification（解除阻塞+已读）', () => {
    const item = {
      id: 'n-confirm',
      title: '需要确认',
      message: '确认执行？',
      priority: 'high' as NotificationPriority,
      category: 'approval',
      isRead: false,
      isBlocking: true,
      actions: [{ label: '确认', action: 'confirm', id: 'act-1' }],
    } as unknown as NotificationItem
    store.setState({ notifications: [item] })
    store.setState({ activeBlockingNotification: item })

    store.getState().executeAction('n-confirm', { action: 'confirm', id: 'act-1' })

    const state = store.getState()
    expect(state.activeBlockingNotification).toBeNull()
    expect(state.notifications.find((n) => n.id === 'n-confirm')?.isRead).toBe(true)
  })
})
