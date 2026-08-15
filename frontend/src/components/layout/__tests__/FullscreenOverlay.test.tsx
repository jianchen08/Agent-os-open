/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：FullscreenOverlay 全屏覆盖层（task_layout_responsive 任务 4）
 *
 * 桌面全屏专注保留：某区域一键全屏、Esc/按钮退出；顶栏样式与轻顶栏统一（≤44px）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { FullscreenOverlay } from '../FullscreenOverlay'

describe('FullscreenOverlay — 桌面全屏专注（保留）', () => {
  it('未激活时不渲染', () => {
    const { container } = render(
      <FullscreenOverlay isActive={false} onExit={() => {}}>
        <div />
      </FullscreenOverlay>,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it('激活时渲染标题 + 内容 + 退出按钮，点击退出回调', () => {
    const onExit = vi.fn()
    render(
      <FullscreenOverlay isActive title="代码审查" onExit={onExit}>
        <div data-testid="fullscreen-content">内容</div>
      </FullscreenOverlay>,
    )

    expect(screen.getByText('代码审查')).toBeInTheDocument()
    expect(screen.getByTestId('fullscreen-content')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /退出全屏/ }))
    expect(onExit).toHaveBeenCalledTimes(1)
  })

  it('顶栏高度与轻顶栏一致（≤44px，CSS 变量控制）', () => {
    render(
      <FullscreenOverlay isActive title="t" onExit={() => {}}>
        <div />
      </FullscreenOverlay>,
    )
    const toolbar = screen.getByTestId('fullscreen-toolbar')
    expect(toolbar).toHaveStyle({ height: 'var(--layout-titlebar-height, 44px)' })
  })
})
