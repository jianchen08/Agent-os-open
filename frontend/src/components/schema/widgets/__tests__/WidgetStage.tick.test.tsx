/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * WidgetStage 的声明迟到轮询补测：挂载后装载的声明经 1.5s 轮询进入组台
 * （否则首屏分组不出现，须手动刷新）。
 *
 * mock 边界：声明 widget 渲染体（重依赖链）。
 */

import { act, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/schema/DeclaredWidgetLayer', () => ({
  DeclaredWidgetLayer: () => <div data-testid="declared-layer" />,
}))

import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { WidgetStage } from '../WidgetStage'

beforeEach(() => {
  contributionRegistry.clear()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  contributionRegistry.clear()
})

describe('WidgetStage — 声明迟到轮询', () => {
  it('挂载后装载的声明 → 轮询后分组出现', async () => {
    const { container } = render(<WidgetStage space="stage-test" />)

    contributionRegistry.registerPresetWidgets([
      {
        id: 'late_widget',
        type: 'status_card',
        space: 'stage-test',
        group: '演示组',
        order: 10,
      } as never,
    ])

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_500)
    })

    expect(container.textContent).toContain('演示组')
  })
})
