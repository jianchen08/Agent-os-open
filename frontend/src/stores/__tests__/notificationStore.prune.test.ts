// @feature: FP-T12 前端适配(X18-F1/P-139 删任务/会话不清关联通知) | @ci: frontend-test
/**
 * X18-F1（P-139）：pruneForTarget 按目标坐标摘除关联通知的契约
 *
 * 修复前：删任务（task_deleted）/删会话（deleteSession）不清通知中心里带该
 * taskId/sessionId 的条目——残留条目点击空路由（维持现状不跳转），条目留驻
 * 到刷新。本文件锁定 store 级契约：
 *
 * - 按 taskId / sessionId 摘除带该坐标的条目，无关条目保留；一次调用摘除
 *   两坐标的并集；
 * - 摘除即 dismissed 记忆（同 id 重投不回魂——与用户移除/清空同语义）；
 * - 带该坐标的阻塞模态一并解除；
 * - 无匹配坐标/空坐标参数：no-op；
 * - persist 面（X14-F1 之上）：摘除后持久化摘要同步不含被摘条目；rehydrate
 *   后不回魂，同 id 重放帧仍被持久化 dismissed 记忆挡住。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { userScopedKey } from '@/utils/userScopedStorage'
import type { NotificationItem } from '@/types/notification'

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
    activeBlockingNotification: NotificationItem | null
    pruneForTarget: (target: { taskId?: string; sessionId?: string }) => void
    addNotification: (data: AddInput) => string
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

/** 读持久化信封里的条目摘要数组 */
function persistedEntryIds(key: string): string[] {
  const raw = localStorage.getItem(key)
  expect(raw).not.toBeNull()
  const state = (JSON.parse(raw as string) as { state: Record<string, unknown> }).state
  return ((state.notifications ?? []) as Array<Record<string, unknown>>).map((e) => e.id as string)
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

beforeEach(() => {
  localStorage.clear()
})

describe('X18-F1 pruneForTarget 摘除带坐标条目', () => {
  it('按 taskId 摘除：带该 taskId 的条目消失，无关 taskId / sessionId / 无坐标条目保留', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-a1', title: '任务A通知1', taskId: 'task-a' }))
    store.getState().addNotification(makeInput({ id: 'ntf-a2', title: '任务A通知2', taskId: 'task-a' }))
    store.getState().addNotification(makeInput({ id: 'ntf-x', title: '别的任务', taskId: 'task-x' }))
    store.getState().addNotification(makeInput({ id: 'ntf-s', title: '会话通知', sessionId: 'sess-1' }))
    store.getState().addNotification(makeInput({ id: 'ntf-bare', title: '无坐标' }))

    store.getState().pruneForTarget({ taskId: 'task-a' })

    const ids = store.getState().notifications.map((n) => n.id)
    expect(ids).not.toContain('ntf-a1')
    expect(ids).not.toContain('ntf-a2')
    expect(ids).toContain('ntf-x')
    expect(ids).toContain('ntf-s')
    expect(ids).toContain('ntf-bare')
    expect(ids).toHaveLength(3)
  })

  it('按 sessionId 摘除：带该 sessionId 的条目消失，taskId 坐标条目不受牵连', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-s1', title: '会话1通知', sessionId: 'sess-1' }))
    store.getState().addNotification(makeInput({ id: 'ntf-s2', title: '会话2通知', sessionId: 'sess-2' }))
    store.getState().addNotification(makeInput({ id: 'ntf-t', title: '任务通知', taskId: 'task-9' }))

    store.getState().pruneForTarget({ sessionId: 'sess-1' })

    const ids = store.getState().notifications.map((n) => n.id)
    expect(ids).not.toContain('ntf-s1')
    expect(ids).toContain('ntf-s2')
    expect(ids).toContain('ntf-t')
  })

  it('一次调用摘除 taskId 与 sessionId 坐标的并集', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-by-task', title: '按任务', taskId: 'task-a' }))
    store.getState().addNotification(makeInput({ id: 'ntf-by-sess', title: '按会话', sessionId: 'sess-b' }))
    store.getState().addNotification(makeInput({ id: 'ntf-other', title: '无关' }))

    store.getState().pruneForTarget({ taskId: 'task-a', sessionId: 'sess-b' })

    expect(store.getState().notifications.map((n) => n.id)).toEqual(['ntf-other'])
  })

  it('摘除即 dismissed 记忆：同 id 重投不回魂（列表管理语义与用户移除一致）', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-dead', title: '将删', taskId: 'task-a' }))
    store.getState().pruneForTarget({ taskId: 'task-a' })

    store.getState().addNotification(makeInput({ id: 'ntf-dead', title: '将删', taskId: 'task-a' }))

    expect(store.getState().notifications).toHaveLength(0)
  })

  it('带该坐标的阻塞模态一并解除', async () => {
    const store = await freshStore()
    store.getState().addNotification(
      makeInput({ id: 'blk-dead', title: '阻塞待删', taskId: 'task-a', isBlocking: true, priority: 'critical' }),
    )
    expect(store.getState().activeBlockingNotification?.id).toBe('blk-dead')

    store.getState().pruneForTarget({ taskId: 'task-a' })

    expect(store.getState().notifications).toHaveLength(0)
    expect(store.getState().activeBlockingNotification).toBeNull()
  })

  it('无匹配坐标与空坐标参数：no-op（列表原封不动）', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-1', title: '甲', taskId: 'task-a' }))
    const before = store.getState().notifications

    store.getState().pruneForTarget({ taskId: 'task-miss' })
    store.getState().pruneForTarget({ sessionId: 'sess-miss' })
    store.getState().pruneForTarget({})
    store.getState().pruneForTarget({ taskId: undefined, sessionId: undefined })

    expect(store.getState().notifications).toBe(before)
  })
})

describe('X18-F1 pruneForTarget 与 persist 面交互', () => {
  it('摘除后持久化摘要同步不含被摘条目；rehydrate 不回魂；同 id 重放帧被持久化 dismissed 记忆挡住', async () => {
    const store = await freshStore()
    store.getState().addNotification(makeInput({ id: 'ntf-keep', title: '保留', sessionId: 'sess-keep' }))
    store.getState().addNotification(makeInput({ id: 'ntf-dead', title: '待摘', taskId: 'task-dead' }))

    store.getState().pruneForTarget({ taskId: 'task-dead' })

    // 落盘面同步：持久化摘要已不含被摘条目
    const key = userScopedKey('notification-storage')
    expect(persistedEntryIds(key)).toEqual(['ntf-keep'])

    // 模拟刷新：rehydrate 后被摘条目不回魂
    const reloaded = await freshStore()
    const ids = reloaded.getState().notifications.map((n) => n.id)
    expect(ids).toContain('ntf-keep')
    expect(ids).not.toContain('ntf-dead')

    // 重放帧（同 id）不因 rehydrate 复活：持久化 dismissed 记忆判重
    reloaded.getState().addNotification(makeInput({ id: 'ntf-dead', title: '待摘', taskId: 'task-dead' }))
    expect(reloaded.getState().notifications.map((n) => n.id)).not.toContain('ntf-dead')
  })
})
