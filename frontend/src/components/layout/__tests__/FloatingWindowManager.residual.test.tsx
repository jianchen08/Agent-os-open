/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * FloatingWindowManager 残余覆盖补测：
 * - 浮窗内容分发兜底：既无 page props、id 也不带 `-popout-` 约定的实例 →
 *   渲染「内容不可用」占位（不崩溃、不误判 pageId）
 * - 标题栏「最小化」按钮 → 以取反值回调宿主（还原入口同钮）
 * - 标题栏「关闭」按钮 → 回调宿主关闭该窗
 *
 * 断言用户可见结果（占位文案 / 宿主收到的更新载荷），不触碰内部拖拽实现。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FloatingWindowManager, renderFloatingWindowContent } from '../FloatingWindowManager'
import type { FloatingWindowInstance } from '@/types/layout'

function makeWindow(overrides: Partial<FloatingWindowInstance> = {}): FloatingWindowInstance {
  return {
    id: 'win-plain',
    title: '笔记浮窗',
    position: { x: 10, y: 20 },
    size: { width: 320, height: 240 },
    zIndex: 10,
    isMinimized: false,
    isMaximized: false,
    ...overrides,
  }
}

describe('renderFloatingWindowContent — 分发兜底', () => {
  it('无 page/组件声明的实例 → 占位并回显标题', () => {
    const node = renderFloatingWindowContent(makeWindow({ title: '笔记浮窗' }))
    render(<div>{node}</div>)

    expect(screen.getByText(/笔记浮窗 - 内容不可用/)).toBeInTheDocument()
  })

  it('标题缺失 → 占位回退实例 id（不出现 undefined）', () => {
    const node = renderFloatingWindowContent(makeWindow({ title: undefined }))
    render(<div>{node}</div>)

    expect(screen.getByText(/win-plain - 内容不可用/)).toBeInTheDocument()
  })
})

describe('FloatingWindowManager — 标题栏操作', () => {
  it('点「最小化」→ 以取反值回调宿主（还原按钮态）', () => {
    const onUpdateWindow = vi.fn()
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={onUpdateWindow}
        onCloseWindow={vi.fn()}
        renderContent={() => <div>内容</div>}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: '−' }))

    expect(onUpdateWindow).toHaveBeenCalledWith('win-plain', { isMinimized: true })
  })

  it('点「关闭」→ 回调宿主关闭该实例', () => {
    const onCloseWindow = vi.fn()
    render(
      <FloatingWindowManager
        windows={[makeWindow()]}
        onUpdateWindow={vi.fn()}
        onCloseWindow={onCloseWindow}
        renderContent={() => <div>内容</div>}
      />,
    )

    const titlebar = screen.getByTestId('floating-window-titlebar')
    const buttons = titlebar.querySelectorAll('button')
    fireEvent.click(buttons[buttons.length - 1])

    expect(onCloseWindow).toHaveBeenCalledWith('win-plain')
  })
})
