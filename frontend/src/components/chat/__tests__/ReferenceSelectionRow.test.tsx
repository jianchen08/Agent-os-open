/** @feature FP-0.2.三 宿主接入 | @ci: frontend-test */
/**
 * 功能测试：ReferenceSelectionRow 输入区引用镜像行（通用 provider 缝）
 *
 * host provider 经 '@/services/references' 入口副作用真实注册，只 mock
 * 外部数据通道 hostBridge——行组件、注册缝、host 适配器全真实执行：
 * - 有选中 → 渲染 label/chips/清除按钮，点击清除调 clearHostSelection
 * - 无选中 → 整行不渲染
 * - 订阅推送 → 行随之出现/消失（实时镜像）
 * - 挂载按 threadId 激活数据通道；切线程重新激活；卸载退订
 * - 缝的通用性：无 getRow 的 provider 不参与渲染；多域行并存
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { registerReferenceProvider } from '@/services/references'
import { ReferenceSelectionRow } from '../ReferenceSelectionRow'
import type { HostSelectionState } from '@/services/host/hostBridge'

const bridge = vi.hoisted(() => {
  const state = {
    current: { connected: false, items: [], signature: '' } as HostSelectionState,
  }
  return {
    state,
    clear: vi.fn(),
    init: vi.fn(),
    listeners: new Set<(s: HostSelectionState) => void>(),
  }
})

vi.mock('@/services/host/hostBridge', () => ({
  getHostSelection: () => bridge.state.current,
  clearHostSelection: (...a: unknown[]) => bridge.clear(...a),
  initHostSelection: (t: string) => bridge.init(t),
  hostPreviewUrl: (i: number, sig: string) => `http://preview/${i}?v=${sig}`,
  subscribeHostSelection: (fn: (s: HostSelectionState) => void) => {
    bridge.listeners.add(fn)
    return () => {
      bridge.listeners.delete(fn)
    }
  },
}))

/** 有选中项的快照 */
const SELECTED: HostSelectionState = {
  connected: true,
  items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player', preview_kind: 'texture' }],
  signature: 'Player@Node2D/Player',
  source: 'godot',
  display_name: 'Godot',
}

/** 第二组区分输入：多条目 */
const SELECTED_TWO: HostSelectionState = {
  connected: true,
  items: [
    { name: 'Enemy', type: 'CharacterBody2D', path: 'Enemies/Enemy' },
    { name: 'Floor', type: 'StaticBody2D', path: 'Level/Floor' },
  ],
  signature: 'Enemy+Floor',
  source: 'godot',
  display_name: 'Godot',
}

/** 推送快照（模拟插件侧选中变化广播） */
function pushState(next: HostSelectionState): void {
  bridge.state.current = next
  bridge.listeners.forEach((fn) => fn(next))
}

beforeEach(() => {
  bridge.state.current = { connected: false, items: [], signature: '' }
  bridge.clear.mockReset().mockResolvedValue(true)
  bridge.init.mockReset()
  bridge.listeners.clear()
})

describe('ReferenceSelectionRow — host provider（经注册缝）', () => {
  it('有选中 → 渲染清除按钮，点击调用 clearHostSelection', () => {
    pushState(SELECTED)
    render(<ReferenceSelectionRow threadId="t1" />)

    expect(screen.getByText('Godot 引用')).toBeInTheDocument()
    expect(screen.getByText('Player')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('reference-selection-clear'))

    expect(bridge.clear).toHaveBeenCalledTimes(1)
  })

  it('清除按钮带 aria-label（可访问性）', () => {
    pushState(SELECTED)
    render(<ReferenceSelectionRow threadId="t1" />)

    expect(screen.getByRole('button', { name: '清除 Godot 引用' })).toBeInTheDocument()
  })

  it('无选中 → 整行不渲染（无清除入口）', () => {
    const { container } = render(<ReferenceSelectionRow threadId="t1" />)

    expect(container.firstChild).toBeNull()
  })

  it('订阅推送 → 行随选中出现/消失，多条目全量镜像', () => {
    const { container } = render(<ReferenceSelectionRow threadId="t1" />)
    expect(container.firstChild).toBeNull()

    act(() => pushState(SELECTED))
    expect(screen.getByText('Player')).toBeInTheDocument()

    act(() => pushState(SELECTED_TWO))
    expect(screen.getByText('Enemy')).toBeInTheDocument()
    expect(screen.getByText('Floor')).toBeInTheDocument()
    expect(screen.queryByText('Player')).not.toBeInTheDocument()

    act(() => pushState({ connected: false, items: [], signature: '' }))
    expect(container.firstChild).toBeNull()
  })

  it('挂载按 threadId 激活数据通道；切线程重新激活；卸载退订', () => {
    const { rerender, unmount } = render(<ReferenceSelectionRow threadId="t1" />)
    expect(bridge.init).toHaveBeenCalledWith('t1')

    rerender(<ReferenceSelectionRow threadId="t2" />)
    expect(bridge.init).toHaveBeenCalledWith('t2')

    expect(bridge.listeners.size).toBeGreaterThan(0)
    unmount()
    expect(bridge.listeners.size).toBe(0)
  })
})

describe('ReferenceSelectionRow — 缝的通用性', () => {
  it('无 getRow 的 provider 不参与渲染；多域行并存', () => {
    const fake = { on: false, notify: () => {} }
    registerReferenceProvider({
      source: 'fake-inject-only',
      getSelection: () => null,
    })
    registerReferenceProvider({
      source: 'fake-row',
      getSelection: () => null,
      getRow: () =>
        fake.on
          ? { label: '假域引用', chips: [{ key: 'k1', kind: 'file', title: '设计稿' }], clear: () => {} }
          : null,
      subscribeRow: (cb: () => void) => {
        fake.notify = cb
        return () => {}
      },
    })

    // host 无选中 + 两假 provider 注册：inject-only 被过滤，fake-row 关闭 → 空
    const { container } = render(<ReferenceSelectionRow threadId="t" />)
    expect(container.firstChild).toBeNull()

    act(() => {
      fake.on = true
      fake.notify()
    })
    expect(screen.getByText('假域引用')).toBeInTheDocument()
    expect(screen.getByText('设计稿')).toBeInTheDocument()

    // host 行与假域行并存（每 provider 一行）
    act(() => pushState(SELECTED))
    expect(screen.getByText('Godot 引用')).toBeInTheDocument()
    expect(screen.getByText('假域引用')).toBeInTheDocument()
  })
})
