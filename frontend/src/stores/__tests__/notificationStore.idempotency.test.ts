/** @feature: FP-T12 前端适配(BUG-74 通知中心幂等闸) | @ci: frontend-test */
/**
 * BUG-74：装机版通知中心单条通知以 ~10s 周期无限重发——/interaction/pending
 * 兜底轮询反复重放同一条 forever-pending 的 notification 模式记录，前端入列
 * 无稳定 id 幂等闸（同 id 重复入列、badge 无限增长、清空后回弹）。
 *
 * 本文件锁定通知入列幂等契约：
 * - 有稳定 id：重复 id 更新而非新增（内容/时间戳以最新投递为准，已读态保持）；
 *   已被用户移除/清空的通知同 id 重投不回弹。
 * - 无 id：同 title+message 短窗内按内容指纹只入一条；窗口外放行。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { NotificationItem } from '@/types/notification'

let store: typeof import('@/stores/notificationStore').useNotificationStore

beforeEach(async () => {
  vi.resetModules()
  vi.useFakeTimers()
  const mod = await import('@/stores/notificationStore')
  store = mod.useNotificationStore
})

afterEach(() => {
  vi.useRealTimers()
})

type AddInput = Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'> & { id?: string }

function makeInput(overrides: Partial<AddInput> = {}): AddInput {
  return {
    title: '写作链交付完成',
    message: 'R245 章节已交付',
    priority: 'normal',
    category: 'alert',
    isBlocking: false,
    sourceLabel: '测试',
    ...overrides,
  }
}

/** 在指定系统时间入队（timestamp 由 store 取当前时间，更新语义断言需要可区分的时间） */
function addAt(epochMs: number, data: AddInput): string {
  vi.setSystemTime(epochMs)
  return store.getState().addNotification(data)
}

describe('BUG-74 有稳定 id：重复 id 更新而非新增', () => {
  it('同 id 重投只保留一条，内容与时间戳以最新投递为准', () => {
    addAt(1_000, makeInput({ id: 'ab3d3fa40d8e', message: '第一版描述' }))
    addAt(11_000, makeInput({ id: 'ab3d3fa40d8e', message: '重投后的描述' }))

    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].message).toBe('重投后的描述')
    expect(new Date(list[0].timestamp).getTime()).toBe(11_000)
  })

  it('同 id 重投不把已读刷回未读（badge 不因重放增长）', () => {
    addAt(1_000, makeInput({ id: 'ab3d3fa40d8e' }))
    store.getState().markAsRead('ab3d3fa40d8e')
    expect(store.getState().getUnreadCount()).toBe(0)

    addAt(11_000, makeInput({ id: 'ab3d3fa40d8e' }))
    expect(store.getState().notifications).toHaveLength(1)
    expect(store.getState().getUnreadCount()).toBe(0)
  })

  it('已入列同 id 重投不重新弹出通知面板', () => {
    addAt(1_000, makeInput({ id: 'ab3d3fa40d8e', priority: 'high' }))
    store.getState().closePanel()
    addAt(11_000, makeInput({ id: 'ab3d3fa40d8e', priority: 'high' }))
    expect(store.getState().isPanelOpen).toBe(false)
  })
})

describe('BUG-74 有稳定 id：用户移除后同 id 重投不回弹', () => {
  it('clearAll 后同 id 重投不入列（清空不被轮询重放推翻）', () => {
    addAt(1_000, makeInput({ id: 'ab3d3fa40d8e' }))
    store.getState().clearAll()
    expect(store.getState().notifications).toHaveLength(0)

    addAt(11_000, makeInput({ id: 'ab3d3fa40d8e' }))
    expect(store.getState().notifications).toHaveLength(0)
  })

  it('dismiss 后同 id 重投不入列', () => {
    addAt(1_000, makeInput({ id: 'ab3d3fa40d8e' }))
    store.getState().dismissNotification('ab3d3fa40d8e')

    addAt(11_000, makeInput({ id: 'ab3d3fa40d8e' }))
    expect(store.getState().notifications).toHaveLength(0)
  })

  it('移除记忆只抑制已移除的 id，新 id 照常入列', () => {
    addAt(1_000, makeInput({ id: 'old-1', title: '旧通知' }))
    store.getState().clearAll()

    addAt(11_000, makeInput({ id: 'new-1', title: '新通知' }))
    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe('new-1')
  })
})

describe('BUG-74 无 id：内容指纹短窗兜底去重', () => {
  it('短窗内同 title+message 重投只入一条，返回首次入列 id', () => {
    const first = addAt(1_000, makeInput())
    const second = addAt(11_000, makeInput())

    expect(second).toBe(first)
    expect(store.getState().notifications).toHaveLength(1)
  })

  it('窗口外同内容重投放行（两条），重投后窗口以最新一次为准', () => {
    addAt(1_000, makeInput())
    // 越过指纹窗口（>30s）后重投：允许再次提醒
    addAt(40_000, makeInput())
    expect(store.getState().notifications).toHaveLength(2)

    // 窗口锚点刷新到第二次：40s+20s 仍在窗内
    addAt(60_000, makeInput())
    expect(store.getState().notifications).toHaveLength(2)
  })

  it('同窗不同内容各自入列', () => {
    addAt(1_000, makeInput({ title: '交付完成' }))
    addAt(2_000, makeInput({ title: '压缩失败', message: '上下文膨胀' }))
    expect(store.getState().notifications).toHaveLength(2)
  })

  it('携带关联坐标的无 id 通知不走指纹闸（按实体的重复告警不吞）', () => {
    // 同 pipeline 的命中率骤降类告警：同 title+message 短窗内重投是真实
    // 的按实体事件，不指纹去重
    addAt(1_000, makeInput({ title: '缓存命中率骤降', sessionId: 'pipe-a' }))
    addAt(2_000, makeInput({ title: '缓存命中率骤降', sessionId: 'pipe-a' }))
    expect(store.getState().notifications).toHaveLength(2)
  })
})
