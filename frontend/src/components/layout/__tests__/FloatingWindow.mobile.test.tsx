/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：FloatingWindowManager 移动端底部 sheet（task_layout_responsive 任务 3/4）
 *
 * 移动端 floating 窗口不再用桌面式固定定位拖拽，改为底部 sheet 滑入
 * （非全屏遮罩，可关闭）：最大化可用性，避免小屏拖拽误触。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FloatingWindowManager } from '../FloatingWindowManager'
import type { FloatingWindowInstance } from '@/types/layout'

function makeWindow(over: Partial<FloatingWindowInstance> = {}): FloatingWindowInstance {
  return {
    id: 'w1',
    title: '成本监控',
    position: { x: 120, y: 80 },
    size: { width: 360, height: 260 },
    zIndex: 10,
    isMinimized: false,
    ...over,
  }
}

const renderContent = (win: FloatingWindowInstance) => (
  <div data-testid={`content-${win.id}`}>{win.title}</div>
)

describe('FloatingWindowManager — 移动端底部 sheet', () => {
  it('移动端：浮窗以底部 sheet 呈现（含标题与关闭）', () => {
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={() => {}}
        onCloseWindow={() => {}}
        renderContent={renderContent}
        isMobile
      />,
    )

    const sheet = screen.getByTestId('floating-sheet')
    expect(sheet).toBeInTheDocument()
    // 标题栏 + 内容区都含窗口名
    expect(screen.getAllByText('成本监控').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByTestId('content-w1')).toBeInTheDocument()
    expect(screen.getByTestId('floating-sheet-close')).toBeInTheDocument()
  })

  it('移动端：sheet 关闭按钮回调 onCloseWindow', () => {
    const onClose = vi.fn()
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={() => {}}
        onCloseWindow={onClose}
        renderContent={renderContent}
        isMobile
      />,
    )

    fireEvent.click(screen.getByTestId('floating-sheet-close'))
    expect(onClose).toHaveBeenCalledWith('w1')
  })

  it('桌面：仍为固定定位窗口（非 sheet），拖拽标题栏移动窗口', () => {
    const onUpdate = vi.fn()
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={onUpdate}
        onCloseWindow={() => {}}
        renderContent={renderContent}
      />,
    )

    expect(screen.queryByTestId('floating-sheet')).not.toBeInTheDocument()
    expect(screen.getByTestId('floating-window')).toBeInTheDocument()

    // 标题栏拖拽：mousedown 记录起点 → mousemove 更新 position
    fireEvent.mouseDown(screen.getByTestId('floating-window-titlebar'), { clientX: 200, clientY: 100 })
    fireEvent.mouseMove(document, { clientX: 240, clientY: 130 })
    fireEvent.mouseUp(document)
    expect(onUpdate).toHaveBeenCalledWith('w1', { position: { x: 160, y: 110 } })
  })

  it('无浮窗时渲染 null', () => {
    const { container } = render(
      <FloatingWindowManager
        windows={[]}
        onUpdateWindow={() => {}}
        onCloseWindow={() => {}}
        renderContent={renderContent}
        isMobile
      />,
    )
    expect(container).toBeEmptyDOMElement()
  })
})
