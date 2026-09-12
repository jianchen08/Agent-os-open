// @feature FP-T12 前端组件补测
/**
 * WidgetStage 测试（widget_stage 声明组台宿主）
 *
 * 核验契约：
 * - props.space 透传 DeclaredWidgetLayer（未声明 space 时兜底 'widget-stage'）
 * - 分组声明（group 字段）→ tab 栏 + 仅渲染激活组的声明（组序=组员最小 order）
 * - 无分组声明 → 平铺（兼容 triggers 等小页，不渲染 tab 栏）
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { WidgetStage } from '../WidgetStage'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

const layerMock = vi.fn((props: { space?: string; declarations?: unknown[] }) => (
  <div data-testid="declared-layer">{(props.declarations ?? []).length} 件</div>
))
vi.mock('@/components/schema/DeclaredWidgetLayer', () => ({
  DeclaredWidgetLayer: (props: { space?: string; declarations?: unknown[] }) => layerMock(props),
}))

function seedWidgets(widgets: Array<Record<string, unknown>>) {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'seed_plugin',
        plugin_name: 'Seed',
        ui_schema: { widgets },
      },
    ],
    plugin_configs: [],
  })
}

beforeEach(() => {
  layerMock.mockClear()
})

describe('WidgetStage — 声明 space 组台', () => {
  it('未分组声明 → 平铺（无 tab 栏，渲染空间全部声明）', () => {
    seedWidgets([{ id: 'a', type: 'table', space: 'triggers' }, { id: 'b', type: 'form', space: 'triggers' }])
    render(<WidgetStage space="triggers" />)
    expect(screen.getByTestId('widget-stage')).toBeInTheDocument()
    expect(screen.queryByTestId(/^widget-stage-tab-/)).not.toBeInTheDocument()
    expect(layerMock).toHaveBeenCalledWith(expect.objectContaining({ space: 'triggers' }))
  })

  it('未声明 space → 兜底 widget-stage 命名空间', () => {
    seedWidgets([])
    render(<WidgetStage />)
    expect(layerMock).toHaveBeenCalledWith(expect.objectContaining({ space: 'widget-stage' }))
  })
})

describe('WidgetStage — 分组 tab（group 声明）', () => {
  beforeEach(() => {
    seedWidgets([
      { id: 'res1', type: 'status_card', space: 'monitoring', group: '资源', order: 10 },
      { id: 'res2', type: 'status_card', space: 'monitoring', group: '资源', order: 20 },
      { id: 'usage1', type: 'table', space: 'monitoring', group: '用量与成本', order: 40 },
      { id: 'task1', type: 'table', space: 'monitoring', group: '任务', order: 50 },
      { id: 'misc', type: 'table', space: 'monitoring', order: 60 },
    ])
  })

  it('tab 栏按组员最小 order 排序，未分组声明归「概览」组', () => {
    render(<WidgetStage space="monitoring" />)
    const tabs = screen.getAllByTestId(/^widget-stage-tab-/).map((el) => el.textContent)
    expect(tabs).toEqual(['资源', '用量与成本', '任务', '概览'])
  })

  it('默认渲染首组，点击 tab 切换渲染组', () => {
    render(<WidgetStage space="monitoring" />)
    expect(screen.getByText('2 件')).toBeInTheDocument() // 资源组 2 件
    fireEvent.click(screen.getByTestId('widget-stage-tab-任务'))
    expect(screen.getByText('1 件')).toBeInTheDocument()
    expect(layerMock).toHaveBeenLastCalledWith(
      expect.objectContaining({
        declarations: [expect.objectContaining({ id: 'task1' })],
      }),
    )
  })

  it('其他空间的分组声明不混入本空间', () => {
    seedWidgets([
      { id: 'mine', type: 'table', space: 'monitoring', group: '资源', order: 10 },
      { id: 'other', type: 'table', space: 'elsewhere', group: '资源', order: 5 },
    ])
    render(<WidgetStage space="monitoring" />)
    expect(layerMock).toHaveBeenLastCalledWith(
      expect.objectContaining({ declarations: [expect.objectContaining({ id: 'mine' })] }),
    )
  })
})
