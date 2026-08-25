/** @feature FP-0.2.三 宿主接入 | @ci: frontend-test */
/**
 * 功能测试：GodotSelectionRow 引用行——清除按钮
 *
 * 点击清除 → 调用 clearGodotSelection（插件清快照+抑制同签名心跳）；
 * 空选中不渲染整行（无清除入口）。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GodotSelectionRow } from '../GodotSelectionRow'

const clearMock = vi.fn()

vi.mock('@/services/godot/selectionBridge', () => ({
  godotPreviewUrl: () => 'http://preview',
  clearGodotSelection: (...a: unknown[]) => clearMock(...a),
}))

const stateMock = vi.hoisted(() => ({ current: { connected: true, items: [], signature: '' } }))

vi.mock('@/hooks/useGodotSelection', () => ({
  useGodotSelection: () => stateMock.current,
}))

beforeEach(() => {
  clearMock.mockReset().mockResolvedValue(true)
})

describe('GodotSelectionRow — 清除引用', () => {
  it('有选中 → 渲染清除按钮，点击调用 clearGodotSelection', () => {
    stateMock.current = {
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player', preview_kind: 'texture' }],
      signature: 'Player@Node2D/Player',
    }
    render(<GodotSelectionRow threadId="t1" />)

    fireEvent.click(screen.getByTestId('godot-selection-clear'))

    expect(clearMock).toHaveBeenCalledTimes(1)
  })

  it('清除按钮带 aria-label（可访问性）', () => {
    stateMock.current = {
      connected: true,
      items: [{ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' }],
      signature: 's',
    }
    render(<GodotSelectionRow threadId="t1" />)

    expect(screen.getByRole('button', { name: '清除 Godot 引用' })).toBeInTheDocument()
  })

  it('无选中 → 整行不渲染（无清除入口）', () => {
    stateMock.current = { connected: true, items: [], signature: '' }
    const { container } = render(<GodotSelectionRow threadId="t1" />)

    expect(container.firstChild).toBeNull()
  })
})
