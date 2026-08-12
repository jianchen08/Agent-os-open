/**
 * DeclaredWidgetLayer 测试 —— ui_schema 声明 widget 的渲染消费
 *
 * 意图：验证「声明 → 实现」链路在渲染侧正确落点：
 * - 已注册 type 渲染对应组件并透传 props
 * - space 过滤
 * - 未解析声明不渲染但可观测（warn）
 * - 无声明时返回 null（不污染宿主布局）
 */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { DeclaredWidgetLayer } from '../DeclaredWidgetLayer'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'

const Stub: WidgetComponent = (props: { label?: string }) => (
  <div data-testid="stub-widget">{props.label}</div>
)

describe('DeclaredWidgetLayer', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('已注册 type 渲染对应组件，透传声明 props', () => {
    widgetRegistry.register('cost_chart', Stub, { name: 'cost_chart', supportedSpaces: ['workspace'] })
    const declarations = [
      { id: 'cw1', type: 'cost_chart', space: 'workspace', props: { label: '月度成本' } },
    ]

    render(<DeclaredWidgetLayer declarations={declarations} space="workspace" />)

    expect(screen.getByTestId('declared-widget-cw1')).toBeInTheDocument()
    expect(screen.getByTestId('stub-widget')).toBeInTheDocument()
    expect(screen.getByText('月度成本')).toBeInTheDocument()
  })

  it('space 过滤：仅渲染匹配空间的声明', () => {
    widgetRegistry.register('a', Stub, { name: 'a', supportedSpaces: ['workspace'] })
    widgetRegistry.register('b', Stub, { name: 'b', supportedSpaces: ['floating'] })
    const declarations = [
      { id: 'd1', type: 'a', space: 'workspace' },
      { id: 'd2', type: 'b', space: 'floating' },
    ]

    render(<DeclaredWidgetLayer declarations={declarations} space="floating" />)

    expect(screen.queryByTestId('declared-widget-d1')).not.toBeInTheDocument()
    expect(screen.getByTestId('declared-widget-d2')).toBeInTheDocument()
  })

  it('未解析声明不渲染，但 console.warn 可观测', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const declarations = [{ id: 'broken', type: 'no_such_type', space: 'workspace' }]

    const { container } = render(<DeclaredWidgetLayer declarations={declarations} space="workspace" />)

    expect(container.querySelector('[data-testid="declared-widget-layer"]')).toBeNull()
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('1 个 widget 声明未解析'),
      expect.arrayContaining([expect.objectContaining({ id: 'broken' })]),
    )
  })

  it('无声明返回 null（不渲染容器，不污染宿主）', () => {
    const { container } = render(<DeclaredWidgetLayer declarations={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('缺省 declarations 时读 contributionRegistry.getAllWidgets（生产消费链路）', () => {
    widgetRegistry.register('chart', Stub, { name: 'chart', supportedSpaces: ['workspace'] })
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'cost-agent',
          name: 'Cost',
          version: '1',
          ui_schema: { widgets: [{ id: 'cw', type: 'chart', space: 'workspace' }] },
        },
      ],
    } as never)

    render(<DeclaredWidgetLayer space="workspace" />)

    expect(screen.getByTestId('declared-widget-cw')).toBeInTheDocument()
  })
})
