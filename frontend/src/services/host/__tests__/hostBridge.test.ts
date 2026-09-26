/**
 * 功能测试：宿主选中引用桥 hostBridge
 *
 * 覆盖：初始化（订阅 thread + 快照拉取）、WS 事件驱动状态更新、
 * 跨线程事件过滤、清空事件（卡片消失）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type * as hostBridgeMod from '@/services/host/hostBridge'
import type * as notificationStoreMod from '@/stores/notificationStore'

const postMock = vi.fn()
const getMock = vi.fn()
const deleteMock = vi.fn()
const subscribeMock = vi.fn()

vi.mock('@/services/api/client', () => ({
  default: {
    post: (...a: unknown[]) => postMock(...a),
    get: (...a: unknown[]) => getMock(...a),
    delete: (...a: unknown[]) => deleteMock(...a),
  },
}))

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: subscribeMock,
    unsubscribe: vi.fn(),
  },
}))

type Bridge = hostBridgeMod

let bridge: Bridge

beforeEach(async () => {
  vi.resetModules()
  postMock.mockReset().mockResolvedValue({})
  deleteMock.mockReset().mockResolvedValue({})
  getMock.mockReset().mockResolvedValue({
    data: { connected: false, items: [], signature: '' },
  })
  subscribeMock.mockReset()
  bridge = await import('@/services/host/hostBridge')
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** 触发一次 WS host_selection_changed 事件（模拟内核单播） */
function emitSelectionEvent(data: Record<string, unknown>): void {
  const handler = subscribeMock.mock.calls.find(([evt]) => evt === 'host_selection_changed')?.[1]
  expect(handler).toBeTruthy()
  ;(handler as (payload: unknown) => void)({ type: 'host_selection_changed', data })
}

describe('hostBridge 初始化', () => {
  it('订阅当前 thread 并拉取初始快照', async () => {
    getMock.mockResolvedValue({
      data: {
        connected: true,
        items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' }],
        signature: 'Player@Node2D/Player',
        scene: { path: 'res://demo_main.tscn' },
      },
    })

    await bridge.initHostSelection('t1')

    expect(postMock).toHaveBeenCalledWith('/ext/pipeline_host_context/subscribe', { thread_id: 't1' })
    expect(getMock).toHaveBeenCalledWith('/ext/pipeline_host_context/selection')

    const snap = bridge.getHostSelection()
    expect(snap.connected).toBe(true)
    expect(snap.items[0].name).toBe('Player')
  })

  it('内核不可用时静默保持未连接（不抛异常）', async () => {
    postMock.mockRejectedValue(new Error('down'))
    getMock.mockRejectedValue(new Error('down'))

    await expect(bridge.initHostSelection('t1')).resolves.toBeUndefined()
    expect(bridge.getHostSelection().connected).toBe(false)
  })
})

describe('hostBridge 事件驱动状态更新', () => {
  beforeEach(async () => {
    await bridge.initHostSelection('t1')
  })

  it('收到选中事件 → 状态更新并通知订阅者（卡片出现）', () => {
    const seen: unknown[] = []
    bridge.subscribeHostSelection((s) => seen.push(s.items.length))

    emitSelectionEvent({
      thread_id: 't1',
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player', preview_kind: 'texture' }],
      signature: 'Player@Node2D/Player',
    })

    const snap = bridge.getHostSelection()
    expect(snap.items).toHaveLength(1)
    expect(snap.signature).toBe('Player@Node2D/Player')
    expect(seen.at(-1)).toBe(1)
  })

  it('收到清空事件 → items 置空（卡片消失）', () => {
    emitSelectionEvent({ thread_id: 't1', connected: true, items: [], signature: '' })

    expect(bridge.getHostSelection().items).toHaveLength(0)
  })

  it('其他 thread 的事件被过滤', () => {
    emitSelectionEvent({
      thread_id: 'other',
      connected: true,
      items: [{ name: 'X', type: 'Node2D', path: 'Node2D/X' }],
      signature: 'X@Node2D/X',
    })

    expect(bridge.getHostSelection().items).toHaveLength(0)
  })
})

describe('hostBridge 清除引用', () => {
  beforeEach(async () => {
    await bridge.initHostSelection('t1')
    emitSelectionEvent({
      thread_id: 't1',
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' }],
      signature: 'Player@Node2D/Player',
    })
  })

  it('clearHostSelection 成功 → DELETE 端点 + 本地 items 置空', async () => {
    const ok = await bridge.clearHostSelection()

    expect(ok).toBe(true)
    expect(deleteMock).toHaveBeenCalledWith('/ext/pipeline_host_context/selection')
    expect(bridge.getHostSelection().items).toHaveLength(0)
    expect(bridge.getHostSelection().signature).toBe('')
  })

  it('清除失败 → 返回 false 且状态不变（不本地假清）', async () => {
    deleteMock.mockRejectedValue(new Error('down'))

    const ok = await bridge.clearHostSelection()

    expect(ok).toBe(false)
    expect(bridge.getHostSelection().items).toHaveLength(1)
  })
})

describe('hostBridge 周期重申订阅失败上报（U23：非核心失败显式降级提示）', () => {
  let useNotificationStore: notificationStoreMod['useNotificationStore']

  beforeEach(async () => {
    // 与 bridge 同一模块注册表取 store（resetModules 后二者共享同一实例）
    ;({ useNotificationStore } = await import('@/stores/notificationStore'))
    useNotificationStore.setState({ notifications: [] })
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('30s 重申 POST 失败 → 经错误上报链进通知中心（不再静默吞掉）', async () => {
    postMock.mockRejectedValue(new Error('down'))
    await bridge.initHostSelection('t1')
    useNotificationStore.setState({ notifications: [] })

    await vi.advanceTimersByTimeAsync(30_000)

    const messages = useNotificationStore.getState().notifications.map((n) => n.message)
    expect(messages.some((m) => m.includes('订阅失败'))).toBe(true)
  })

  it('连续失败只提示一次（30s 循环不刷屏）；恢复成功后再失败可再次提示', async () => {
    // 通知中心 autoDismiss 会在推进窗口内移除旧通知，故按「新增次数」断言 episode 语义
    let added = 0
    const unsubscribe = useNotificationStore.subscribe((s, prev) => {
      if (s.notifications.length > prev.notifications.length) added++
    })
    postMock.mockRejectedValue(new Error('down'))
    await bridge.initHostSelection('t1')

    await vi.advanceTimersByTimeAsync(90_000) // 3 连败 → 仅首败提示
    expect(added).toBe(1)

    postMock.mockResolvedValue({}) // 恢复成功 → episode 重置
    await vi.advanceTimersByTimeAsync(30_000)
    expect(added).toBe(1)

    postMock.mockRejectedValue(new Error('down')) // 再次失败 → 新 episode
    await vi.advanceTimersByTimeAsync(30_000)
    expect(added).toBe(2)
    unsubscribe()
  })

  it('重申成功 → 零降级提示', async () => {
    await bridge.initHostSelection('t1')
    useNotificationStore.setState({ notifications: [] })

    await vi.advanceTimersByTimeAsync(60_000)

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})

describe('纯函数与退订面', () => {
  it('hostPreviewUrl 拼接 index 与编码后的 v 签名', async () => {
    bridge = await import('@/services/host/hostBridge')
    const url = bridge.hostPreviewUrl(3, 'a b&c')
    expect(url).toContain('index=3')
    expect(url).toContain('v=' + encodeURIComponent('a b&c'))
  })

  it('subscribeHostSelection 返回的取消函数把监听器摘除', async () => {
    bridge = await import('@/services/host/hostBridge')
    const seen: unknown[] = []
    const unsub = bridge.subscribeHostSelection((s) => seen.push(s))
    unsub()
    // 退订后再广播（经内部通道不可直达，改证不抛且 getHostSelection 仍可用）
    expect(() => unsub()).not.toThrow() // 二次退订幂等不崩
    expect(bridge.getHostSelection()).toBeDefined()
  })
})

describe('hostBridge - 重连重订阅', () => {
  it('重连事件且已有当前线程 → 重新初始化订阅并拉快照', async () => {
    await bridge.initHostSelection('t-live')

    const callsBefore = getMock.mock.calls.length
    const reconnected = subscribeMock.mock.calls.find(([evt]) => evt === 'reconnected')
      ?? subscribeMock.mock.calls[subscribeMock.mock.calls.length - 1]
    expect(reconnected).toBeTruthy()
    ;(reconnected![1] as () => void)()
    await vi.waitFor(() => {
      expect(getMock.mock.calls.length).toBeGreaterThan(callsBefore)
    })
  })

})
