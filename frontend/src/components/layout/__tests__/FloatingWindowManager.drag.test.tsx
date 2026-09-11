/**
 * FloatingWindowManager 桌面拖拽行为测试
 *
 * 拖拽经 document 级 mousemove/mouseup 监听实现——组件卸载必须移除监听，
 * 否则拖拽中卸载后监听器泄漏，后续全局 mousemove 仍驱动 onUpdateWindow。
 */
import { fireEvent, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FloatingWindowManager } from '../FloatingWindowManager'
import type { FloatingWindowInstance } from '@/types/layout'

function makeWindow(): FloatingWindowInstance {
  return {
    id: 'win-1',
    title: '悬浮窗',
    component: 'stub',
    position: { x: 10, y: 20 },
    size: { width: 320, height: 240 },
    zIndex: 10,
    isMinimized: false,
    isMaximized: false,
  }
}

const renderContent = () => <div>内容</div>

describe('FloatingWindowManager 拖拽', () => {
  it('拖拽中更新位置；组件卸载后 document 监听不再驱动 onUpdateWindow', () => {
    const onUpdateWindow = vi.fn()
    const { getByTestId, unmount } = render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={onUpdateWindow}
        onCloseWindow={vi.fn()}
        renderContent={renderContent}
      />,
    )

    // 开始拖拽 + 移动 → 位置更新
    fireEvent.mouseDown(getByTestId('floating-window-titlebar'), { clientX: 100, clientY: 100 })
    fireEvent.mouseMove(document, { clientX: 140, clientY: 60 })
    expect(onUpdateWindow).toHaveBeenCalledWith('win-1', { position: { x: 50, y: -20 } })

    // 拖拽中（未 mouseup）组件卸载 → 监听必须随之移除
    unmount()
    const callsAfterUnmount = onUpdateWindow.mock.calls.length
    fireEvent.mouseMove(document, { clientX: 200, clientY: 200 })
    expect(onUpdateWindow.mock.calls.length).toBe(callsAfterUnmount)
  })
})
