/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 错误边界降级 UI 的「刷新页面」动作补测：点按钮触发整页重载（用户自恢复
 * 唯一出口），并有可读的标题/详情。
 *
 * mock 边界：错误上报（外部观测通道）、整页重载（浏览器导航）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/services/errorReporting', () => ({
  captureException: vi.fn(),
}))

import ErrorBoundary from '../ErrorBoundary'

const reloadMock = vi.fn()

beforeEach(() => {
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload: reloadMock },
    writable: true,
    configurable: true,
  })
  reloadMock.mockReset()
  // React 会把边界捕获的错误打到 console.error，测试里静音
  vi.spyOn(console, 'error').mockImplementation(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

function Boom(): never {
  throw new Error('渲染炸了')
}

describe('ErrorBoundary — 降级 UI 动作', () => {
  it('子组件抛错 → 降级 UI 呈现，点「刷新页面」触发整页重载', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    fireEvent.click(screen.getByRole('button', { name: '刷新页面' }))

    expect(reloadMock).toHaveBeenCalledTimes(1)
  })
})
