/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：插件状态项迁移（task_layout_responsive 任务 2）
 *
 * StatusBar 删除后，插件贡献的 dock 空间 + status 栏位项迁移到侧栏底部
 * （`sidebar-plugin-status` 条带）。逻辑与原 StatusBar 一致：
 * - getPagesBySpace('dock') + slot==='status'，经 when 过滤
 * - 动态文案优先 widgetEventStore.latest.data，兜底 item.title
 * - 无项时不渲染（不占空间）
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { PluginStatusItems } from '../StatusItems'

function registerPage(overrides: Partial<PageDeclaration>): void {
  contributionRegistry.register({
    type: 'pages',
    id: overrides.id ?? 'p1',
    space: 'dock',
    ...overrides,
  } as PageDeclaration)
}

describe('PluginStatusItems — dock/status 插件项迁移到侧栏底部', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useWidgetEventStore.setState({ events: {}, latest: {} })
    useContextKeys.setState({ keys: {} })
  })

  it('dock/status 页渲染为状态条目；slot=item 页不渲染', () => {
    registerPage({ id: 'st1', title: '我的状态', slot: 'status' })
    registerPage({ id: 'it1', title: '普通条目', slot: 'item' })

    render(<PluginStatusItems />)

    expect(screen.getByText('我的状态')).toBeInTheDocument()
    expect(screen.queryByText('普通条目')).not.toBeInTheDocument()
  })

  it('when 条件不满足的 status 页隐藏', () => {
    registerPage({ id: 'st2', title: '条件状态', slot: 'status', when: 'no.such.key' })

    render(<PluginStatusItems />)

    expect(screen.queryByText('条件状态')).not.toBeInTheDocument()
  })

  it('widget 事件文案优先：latest.data.label 覆盖 item.title', () => {
    registerPage({ id: 'st3', title: '成本', slot: 'status', widget: 'cost-widget' })
    useWidgetEventStore.getState().dispatchWidgetEvent({
      widget_id: 'cost-widget',
      type: 'update',
      data: { label: '¥12.50' },
    } as never)

    render(<PluginStatusItems />)

    expect(screen.getByText(/¥12\.50/)).toBeInTheDocument()
  })

  it('无插件状态项时不渲染条带（无异常不占空间）', () => {
    render(<PluginStatusItems />)
    expect(screen.queryByTestId('sidebar-plugin-status')).not.toBeInTheDocument()
  })
})
