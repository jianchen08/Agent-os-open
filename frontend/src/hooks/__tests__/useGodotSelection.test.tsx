// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * useGodotSelection 行为测试（此前 0% 覆盖）
 *
 * 该 hook 的职责就是与 selectionBridge（Godot 选中引用桥）的接线：
 * - 初始快照来自 getGodotSelection()（同步读当前状态）
 * - 订阅 selectionBridge 状态变化，推送到达即刷新组件状态
 * - threadId 存在时调 initGodotSelection(threadId) 订阅该线程并拉快照；
 *   缺失时不订阅（无键可查）
 * - 卸载时退订（返回的取消函数被调用）
 * - threadId 切换时重新订阅 + 重新初始化
 *
 * 测试策略：只 mock 外部协作边界（selectionBridge 服务模块），hook 真实执行。
 */

import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useGodotSelection } from '../useGodotSelection'
import type { GodotSelectionState } from '@/services/godot/selectionBridge'

const bridge = vi.hoisted(() => ({
  getGodotSelection: vi.fn(),
  initGodotSelection: vi.fn(),
  subscribeGodotSelection: vi.fn(),
}))

vi.mock('@/services/godot/selectionBridge', () => ({
  getGodotSelection: () => bridge.getGodotSelection(),
  initGodotSelection: (threadId: string) => bridge.initGodotSelection(threadId),
  subscribeGodotSelection: (fn: (s: GodotSelectionState) => void) =>
    bridge.subscribeGodotSelection(fn),
}))

/** 未连接空快照 */
const DISCONNECTED: GodotSelectionState = { connected: false, items: [], signature: '' }

/** 有选中项的快照（区分输入：内容与签名均不同） */
const SELECTED: GodotSelectionState = {
  connected: true,
  items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player', preview_kind: 'texture' }],
  signature: 'Player@Node2D/Player',
  engine_version: '4.4',
}

/** 另一份选中快照（第二组区分输入：不同签名与条目数） */
const SELECTED_TWO: GodotSelectionState = {
  connected: true,
  items: [
    { name: 'Enemy', type: 'CharacterBody2D', path: 'Enemies/Enemy' },
    { name: 'Floor', type: 'StaticBody2D', path: 'Level/Floor' },
  ],
  signature: 'Enemy+Floor',
}

let unsubscribe: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.resetAllMocks()
  unsubscribe = vi.fn()
  bridge.getGodotSelection.mockReturnValue(DISCONNECTED)
  bridge.initGodotSelection.mockResolvedValue(undefined)
  bridge.subscribeGodotSelection.mockReturnValue(unsubscribe)
})

/** 取 hook 注册到 bridge 的监听函数（订阅契约的回调入参） */
function capturedListener(): (s: GodotSelectionState) => void {
  const listener = bridge.subscribeGodotSelection.mock.calls[0]?.[0] as
    | ((s: GodotSelectionState) => void)
    | undefined
  if (!listener) throw new Error('hook 未向 selectionBridge 注册监听')
  return listener
}

describe('useGodotSelection — selectionBridge 接线', () => {
  it('初始状态直接取 bridge 当前快照（未连接 / 有选中两组区分输入）', () => {
    const empty = renderHook(() => useGodotSelection('thread-a'))
    expect(empty.result.current).toEqual(DISCONNECTED)
    empty.unmount()

    bridge.getGodotSelection.mockReturnValue(SELECTED)
    const selected = renderHook(() => useGodotSelection('thread-a'))
    expect(selected.result.current.connected).toBe(true)
    expect(selected.result.current.items).toEqual(SELECTED.items)
    expect(selected.result.current.signature).toBe('Player@Node2D/Player')
  })

  it('bridge 推送新快照 → 组件状态同步刷新（两次推送各校验一次）', () => {
    const { result } = renderHook(() => useGodotSelection('thread-a'))
    const listener = capturedListener()

    act(() => listener(SELECTED))
    expect(result.current.items.map((i) => i.name)).toEqual(['Player'])

    act(() => listener(SELECTED_TWO))
    expect(result.current.items.map((i) => i.name)).toEqual(['Enemy', 'Floor'])
    expect(result.current.signature).toBe('Enemy+Floor')
  })

  it('threadId 存在 → initGodotSelection 收到该 threadId', async () => {
    renderHook(() => useGodotSelection('thread-42'))

    expect(bridge.initGodotSelection).toHaveBeenCalledWith('thread-42')
    await act(async () => {})
  })

  it('threadId 缺失 → 不初始化（无键可查），但仍建立订阅', () => {
    renderHook(() => useGodotSelection(undefined))

    expect(bridge.initGodotSelection).not.toHaveBeenCalled()
    expect(bridge.subscribeGodotSelection).toHaveBeenCalled()
  })

  it('threadId 切换 → 旧订阅退订并按新 threadId 重新初始化', () => {
    const { rerender } = renderHook(({ id }: { id: string }) => useGodotSelection(id), {
      initialProps: { id: 'thread-a' },
    })
    expect(bridge.initGodotSelection).toHaveBeenCalledWith('thread-a')

    rerender({ id: 'thread-b' })

    expect(unsubscribe).toHaveBeenCalled()
    expect(bridge.initGodotSelection).toHaveBeenCalledWith('thread-b')
  })

  it('卸载 → 调用订阅返回的取消函数（不留悬挂监听）', () => {
    const { unmount } = renderHook(() => useGodotSelection('thread-a'))
    expect(unsubscribe).not.toHaveBeenCalled()

    unmount()

    expect(unsubscribe).toHaveBeenCalled()
  })
})
