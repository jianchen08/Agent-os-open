// @feature FP-T12 前端适配(装机事故批次3 通知常驻) | @ci: frontend-test
/**
 * 连接类错误通知常驻/合并/清除契约测试（2026-11-18 装机事故批次 3）。
 *
 * 事故形态：断连 73 分钟里「消息发送失败」「服务请求失败」各按 10s 瞬态
 * 逐条上报（autoDismissMs>0 且非阻塞 → 到点消失且不持久化），用户全程
 * 无常驻可见面；同时高频失败各自成条刷屏风险。
 *
 * 本文件锁定四条行为契约：
 *  - error 级常驻错误（无 autoDismissMs）不自动消失，刷新后随持久化面恢复；
 *  - info 级瞬态通知（autoDismissMs）保持既有自动消失行为不变；
 *  - mergeKey 同键重复投递合并为单条更新（77 次重连不刷 77 条）；
 *  - clearConnectionErrorNotifications 清除连接类存量（connection# 条件条目
 *    + svc-request-failed / conn-self-heal 合并键），消息发送失败保留，
 *    且系统清除不记 dismissed（同键后续失败不被永久静默）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { userScopedKey } from '@/utils/userScopedStorage'
import type { NotificationItem } from '@/types/notification'

/** 本文件消费的 store 能力面（结构化声明，避免动态 import 类型注解） */
interface NotificationStoreApi {
  getState: () => {
    notifications: NotificationItem[]
    addNotification: (data: AddInput) => string
    dismissNotification: (id: string) => void
    clearConnectionErrorNotifications: () => void
  }
}

type AddInput = Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'> & { id?: string }

let store: NotificationStoreApi

beforeEach(async () => {
  // persist 落盘跨测试泄漏：先清存储再重建 store（idempotency.test.ts 同款隔离）
  localStorage.clear()
  vi.resetModules()
  vi.useFakeTimers()
  const mod = await import('@/stores/notificationStore')
  store = mod.useNotificationStore
})

afterEach(() => {
  vi.useRealTimers()
})

function add(data: AddInput): string {
  return store.getState().addNotification(data)
}

describe('error 级常驻通知不自动消失', () => {
  it('服务请求失败（mergeKey 无 autoDismissMs）60s 后仍在列（事故回归：原 10s 即逝）', () => {
    add({
      title: '服务请求失败',
      message: 'WS 票据获取失败',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    vi.advanceTimersByTime(60_000)
    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].title).toBe('服务请求失败')
  })

  it('消息发送失败常驻（mergeKey 无 autoDismissMs）不消失且可被手动关闭', () => {
    const id = add({
      title: '消息发送失败',
      message: '连接断开超过 20s，消息未送达已撤回',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'msg-send-failed',
    })
    vi.advanceTimersByTime(60_000)
    expect(store.getState().notifications).toHaveLength(1)
    // 红线③：常驻通知能被用户手动关掉
    store.getState().dismissNotification(id)
    expect(store.getState().notifications).toHaveLength(0)
  })
})

describe('info 级瞬态通知保持既有自动消失', () => {
  it('autoDismissMs 的 info 通知到点移除（红线①：既有行为不变）', () => {
    add({
      title: '后台任务已完成',
      message: '章节已交付',
      priority: 'normal',
      category: 'info',
      isBlocking: false,
      autoDismissMs: 8000,
    })
    vi.advanceTimersByTime(7_999)
    expect(store.getState().notifications).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(store.getState().notifications).toHaveLength(0)
  })
})

describe('常驻错误通知持久化（刷新后仍在，判据④）', () => {
  it('常驻错误条目进持久化信封且 mergeKey 随摘要落盘；info 瞬态不落盘', () => {
    add({
      title: '服务请求失败',
      message: '取票失败',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    add({
      title: '后台任务已完成',
      priority: 'normal',
      category: 'info',
      isBlocking: false,
      autoDismissMs: 8000,
    })
    const raw = localStorage.getItem(userScopedKey('notification-storage'))
    expect(raw).toBeTruthy()
    const entries = (JSON.parse(raw as string) as {
      state: { notifications: Array<{ id: string; title: string; mergeKey?: string }> }
    }).state.notifications
    const persisted = entries.map((e) => e.title)
    expect(persisted).toContain('服务请求失败')
    expect(persisted).not.toContain('后台任务已完成')
    expect(entries.find((e) => e.title === '服务请求失败')?.mergeKey).toBe('svc-request-failed')
  })
})

describe('mergeKey 高频失败合并为单条更新', () => {
  it('同键两次投递 → 列表恒 1 条，message/时间戳更新为最新（77 次重连不刷屏）', () => {
    vi.setSystemTime(1_000)
    add({
      title: '服务请求失败',
      message: '第 1 次取票失败',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    vi.setSystemTime(2_000)
    const secondId = add({
      title: '服务请求失败',
      message: '第 2 次取票失败',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].id).toBe(secondId)
    expect(list[0].message).toBe('第 2 次取票失败')
  })

  it('用户手动关闭后同键新失败以新 id 重新入列（不被 dismissed 永久静默）', () => {
    const firstId = add({
      title: '服务请求失败',
      message: '第一次故障',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    store.getState().dismissNotification(firstId)
    const secondId = add({
      title: '服务请求失败',
      message: '第二次故障',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      mergeKey: 'svc-request-failed',
    })
    expect(secondId).not.toBe(firstId)
    const list = store.getState().notifications
    expect(list).toHaveLength(1)
    expect(list[0].message).toBe('第二次故障')
  })
})

describe('clearConnectionErrorNotifications 重连成功清除存量', () => {
  it('清除 connection# 条件条目与 svc-request-failed / conn-self-heal，保留消息发送失败', () => {
    add({ id: 'connection#3', title: '多次重连失败（已尝试 77 次）', priority: 'critical', category: 'error', isBlocking: false })
    add({ id: 'connection#1', title: '刷新后恢复的孤儿条目', priority: 'high', category: 'error', isBlocking: false })
    add({ title: '服务请求失败', message: '取票失败', priority: 'high', category: 'error', isBlocking: false, mergeKey: 'svc-request-failed' })
    add({ title: '连接通道已重建，正在重连', message: '桌面壳已重建网络连接池', priority: 'high', category: 'error', isBlocking: false, mergeKey: 'conn-self-heal' })
    add({ title: '消息发送失败', message: '请重新发送', priority: 'high', category: 'error', isBlocking: false, mergeKey: 'msg-send-failed' })
    add({ title: '预算告警', priority: 'high', category: 'alert', isBlocking: false })

    store.getState().clearConnectionErrorNotifications()

    const remaining = store.getState().notifications
    expect(remaining.map((n) => n.title).sort()).toEqual(['消息发送失败', '预算告警'])
  })

  it('系统清除不记 dismissedIds：connection 条件新 episode 与同键新失败可再入列', () => {
    add({ id: 'connection#1', title: '断连', priority: 'critical', category: 'error', isBlocking: false })
    add({ title: '服务请求失败', message: '故障', priority: 'high', category: 'error', isBlocking: false, mergeKey: 'svc-request-failed' })
    store.getState().clearConnectionErrorNotifications()
    expect(store.getState().notifications).toHaveLength(0)

    // 同 id / 同键重报不回弹被挡（dismissed 语义）——系统清除不得产生该记忆
    const reAdded = add({ id: 'connection#1', title: '断连（新 episode）', priority: 'critical', category: 'error', isBlocking: false })
    const reAddedSvc = add({ title: '服务请求失败', message: '新故障', priority: 'high', category: 'error', isBlocking: false, mergeKey: 'svc-request-failed' })
    expect(store.getState().notifications).toHaveLength(2)
    expect(reAdded).toBe('connection#1')
    expect(reAddedSvc).not.toBe('')
  })
})
