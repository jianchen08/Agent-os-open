/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 声明加载迟到捕获的轮询分支（调试中心与 widget 组台同模式）：
 * 注册表在组件挂载后才装载声明时，1.5s 轮询重算条目数并刷新视图——
 * 不轮询则首屏「暂无」会一直停留到用户切页。
 *
 * mock 边界：页面内容渲染体（PageRenderer 重依赖链）。
 */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/schema/PageRenderer', () => ({
  renderPageContent: (page: { id: string }) => <div data-testid="page-body">{page.id}</div>,
}))

import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { DebugCenterHubWidget } from '../DebugCenterHubWidget'

beforeEach(() => {
  contributionRegistry.clear()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  contributionRegistry.clear()
})

describe('DebugCenterHubWidget — 迟到声明轮询捕获', () => {
  it('挂载后才装载的 debug_center tab → 轮询后出现子页切换按钮', async () => {
    render(<DebugCenterHubWidget />)
    expect(screen.getByText('暂无子页')).toBeInTheDocument()

    // 装载迟到（挂载后才注册）
    contributionRegistry.register({
      type: 'pages',
      id: 'debug-tasks',
      title: '任务调试',
      space: 'debug_center',
      slot: 'tab',
      pluginId: 'debug_center',
    } as never)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500)
    })

    expect(screen.getByTestId('debug-hub-tab-debug-tasks')).toBeInTheDocument()
  })
})
