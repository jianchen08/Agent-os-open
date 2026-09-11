/**
 * 通知面板自动弹层行为回归（GUI 黑盒测试 2026-09-11 发现）：
 *
 * 高优 + autoDismissMs 的 toast 类瞬态通知（全部生产方都是 isBlocking:false +
 * autoDismissMs 的形态）曾触发 isPanelOpen=true——全屏抽屉弹出遮挡整页，且
 * 通知到点自动消失后抽屉仍开着（"暂无通知"空屉挡页面）。回归契约：
 * - 瞬态通知（带 autoDismissMs）：只进未读列表，永不自动弹抽屉；
 * - 持久重要通知（无 autoDismissMs 的 high/critical）：保留自动弹层设计；
 * - 瞬态通知到点自动消失（TTL 行为本身保留）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

let store: typeof import('@/stores/notificationStore').useNotificationStore

beforeEach(async () => {
  vi.resetModules()
  const mod = await import('@/stores/notificationStore')
  store = mod.useNotificationStore
  store.getState().clearAll()
  store.setState({ isPanelOpen: false, activeBlockingNotification: null })
})

describe('通知面板自动弹层', () => {
  it('瞬态高优通知（autoDismissMs）不自动弹抽屉，只计未读', () => {
    store.getState().addNotification({
      title: '消息发送失败',
      message: '连接断开超过 20s',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '前端',
    })
    expect(store.getState().isPanelOpen).toBe(false)
    expect(store.getState().notifications).toHaveLength(1)
    expect(store.getState().notifications[0].isRead).toBe(false)
  })

  it('持久重要通知（无 autoDismissMs 的 critical）保留自动弹层', () => {
    store.getState().addNotification({
      title: '服务异常',
      message: '内核不可达',
      priority: 'critical',
      category: 'error',
      isBlocking: false,
      sourceLabel: '系统',
    })
    expect(store.getState().isPanelOpen).toBe(true)
  })

  it('用户已打开抽屉时瞬态高优通知不改变面板状态', () => {
    store.getState().openPanel()
    store.getState().addNotification({
      title: '消息发送失败',
      message: '连接断开超过 20s',
      priority: 'high',
      category: 'error',
      isBlocking: false,
      autoDismissMs: 6000,
      sourceLabel: '前端',
    })
    expect(store.getState().isPanelOpen).toBe(true)
    expect(store.getState().notifications).toHaveLength(1)
  })

  it('瞬态通知到点自动消失（TTL 保留）', () => {
    vi.useFakeTimers()
    try {
      store.getState().addNotification({
        title: '消息发送失败',
        message: '连接断开超过 20s',
        priority: 'high',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '前端',
      })
      expect(store.getState().notifications).toHaveLength(1)
      vi.advanceTimersByTime(6100)
      expect(store.getState().notifications).toHaveLength(0)
    } finally {
      vi.useRealTimers()
    }
  })
})
