/**
 * 功能测试：Godot 选中引用桥 selectionBridge
 *
 * 覆盖：初始化（订阅 thread + 快照拉取）、WS 事件驱动状态更新、
 * 跨线程事件过滤、清空事件（卡片消失）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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

type Bridge = typeof import('@/services/godot/selectionBridge')

let bridge: Bridge

beforeEach(async () => {
  vi.resetModules()
  postMock.mockReset().mockResolvedValue({})
  deleteMock.mockReset().mockResolvedValue({})
  getMock.mockReset().mockResolvedValue({
    data: { connected: false, items: [], signature: '' },
  })
  subscribeMock.mockReset()
  bridge = await import('@/services/godot/selectionBridge')
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** 触发一次 WS godot_selection_changed 事件（模拟内核单播） */
function emitSelectionEvent(data: Record<string, unknown>): void {
  const handler = subscribeMock.mock.calls.find(([evt]) => evt === 'godot_selection_changed')?.[1]
  expect(handler).toBeTruthy()
  ;(handler as (payload: unknown) => void)({ type: 'godot_selection_changed', data })
}

describe('selectionBridge 初始化', () => {
  it('订阅当前 thread 并拉取初始快照', async () => {
    getMock.mockResolvedValue({
      data: {
        connected: true,
        items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' }],
        signature: 'Player@Node2D/Player',
        scene: { path: 'res://demo_main.tscn' },
      },
    })

    await bridge.initGodotSelection('t1')

    expect(postMock).toHaveBeenCalledWith('/ext/pipeline_godot_context/subscribe', { thread_id: 't1' })
    expect(getMock).toHaveBeenCalledWith('/ext/pipeline_godot_context/selection')

    const snap = bridge.getGodotSelection()
    expect(snap.connected).toBe(true)
    expect(snap.items[0].name).toBe('Player')
  })

  it('内核不可用时静默保持未连接（不抛异常）', async () => {
    postMock.mockRejectedValue(new Error('down'))
    getMock.mockRejectedValue(new Error('down'))

    await expect(bridge.initGodotSelection('t1')).resolves.toBeUndefined()
    expect(bridge.getGodotSelection().connected).toBe(false)
  })
})

describe('selectionBridge 事件驱动状态更新', () => {
  beforeEach(async () => {
    await bridge.initGodotSelection('t1')
  })

  it('收到选中事件 → 状态更新并通知订阅者（卡片出现）', () => {
    const seen: unknown[] = []
    bridge.subscribeGodotSelection((s) => seen.push(s.items.length))

    emitSelectionEvent({
      thread_id: 't1',
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player', preview_kind: 'texture' }],
      signature: 'Player@Node2D/Player',
    })

    const snap = bridge.getGodotSelection()
    expect(snap.items).toHaveLength(1)
    expect(snap.signature).toBe('Player@Node2D/Player')
    expect(seen.at(-1)).toBe(1)
  })

  it('收到清空事件 → items 置空（卡片消失）', () => {
    emitSelectionEvent({ thread_id: 't1', connected: true, items: [], signature: '' })

    expect(bridge.getGodotSelection().items).toHaveLength(0)
  })

  it('其他 thread 的事件被过滤', () => {
    emitSelectionEvent({
      thread_id: 'other',
      connected: true,
      items: [{ name: 'X', type: 'Node2D', path: 'Node2D/X' }],
      signature: 'X@Node2D/X',
    })

    expect(bridge.getGodotSelection().items).toHaveLength(0)
  })
})

describe('selectionBridge 清除引用', () => {
  beforeEach(async () => {
    await bridge.initGodotSelection('t1')
    emitSelectionEvent({
      thread_id: 't1',
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' }],
      signature: 'Player@Node2D/Player',
    })
  })

  it('clearGodotSelection 成功 → DELETE 端点 + 本地 items 置空', async () => {
    const ok = await bridge.clearGodotSelection()

    expect(ok).toBe(true)
    expect(deleteMock).toHaveBeenCalledWith('/ext/pipeline_godot_context/selection')
    expect(bridge.getGodotSelection().items).toHaveLength(0)
    expect(bridge.getGodotSelection().signature).toBe('')
  })

  it('清除失败 → 返回 false 且状态不变（不本地假清）', async () => {
    deleteMock.mockRejectedValue(new Error('down'))

    const ok = await bridge.clearGodotSelection()

    expect(ok).toBe(false)
    expect(bridge.getGodotSelection().items).toHaveLength(1)
  })
})
