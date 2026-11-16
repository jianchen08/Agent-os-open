/**
 * FloatingWindowManager 桌面拖拽行为测试
 *
 * 拖拽经 document 级 mousemove/mouseup 监听实现——组件卸载必须移除监听，
 * 否则拖拽中卸载后监听器泄漏，后续全局 mousemove 仍驱动 onUpdateWindow。
 */
import { fireEvent, render, screen } from '@testing-library/react'
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
    fireEvent.mouseMove(document, { clientX: 140, clientY: 60, buttons: 1 })
    expect(onUpdateWindow).toHaveBeenCalledWith('win-1', { position: { x: 50, y: -20 } })

    // 拖拽中（未 mouseup）组件卸载 → 监听必须随之移除
    unmount()
    const callsAfterUnmount = onUpdateWindow.mock.calls.length
    fireEvent.mouseMove(document, { clientX: 200, clientY: 200, buttons: 1 })
    expect(onUpdateWindow.mock.calls.length).toBe(callsAfterUnmount)
  })

  it('抬起事件被内嵌文档吞掉时必须收尾：后续移动不再驱动位置更新', () => {
    // 病灶同布局手柄：指针抬起落在内嵌文档（iframe/webview）上时，宿主收不到
    // mouseup——浮窗此后会一直跟着鼠标跑。判据：移动事件 buttons===0 即收尾。
    const onUpdateWindow = vi.fn()
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={onUpdateWindow}
        onCloseWindow={vi.fn()}
        renderContent={renderContent}
      />,
    )

    fireEvent.mouseDown(screen.getByTestId('floating-window-titlebar'), { clientX: 100, clientY: 100 })
    fireEvent.mouseMove(document, { clientX: 140, clientY: 60, buttons: 1 })
    expect(onUpdateWindow).toHaveBeenCalled()

    // 抬起事件丢失：宿主只收到「没有按键」的移动事件 → 必须收尾
    fireEvent.mouseMove(document, { clientX: 200, clientY: 200, buttons: 0 })
    const callsAtRelease = onUpdateWindow.mock.calls.length
    fireEvent.mouseMove(document, { clientX: 300, clientY: 300, buttons: 0 })
    expect(onUpdateWindow.mock.calls.length).toBe(callsAtRelease)
  })
})
