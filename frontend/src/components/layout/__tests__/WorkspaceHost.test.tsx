/**
 * 功能测试：声明驱动的工作区渲染（架构愿景功能点）
 *
 * 推演链：项目愿景（插件声明能力）→ 前端愿景（声明驱动渲染）→ 架构（workspace
 * SpaceHost 必须消费 ui_schema 声明的 widget）→ 功能点（"插件声明的工作区 widget，
 * 在 FiveSpaceLayout 的工作区里真渲染出来"）。
 *
 * WorkspaceHost 是 FiveSpaceLayout 实际挂载的工作区宿主（= WorkspacePanel +
 * DeclaredWidgetLayer）。本测试渲染真实 WorkspaceHost + 真实注册表，断言声明 widget
 * 的内容出现在 DOM——证明架构链路在 app 工作区**通电**（不再只活在源码里）。
 *
 * 关联：docs/working/重要设计/前端能力统一架构.md §5.3 / §5.4
 */

import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { WorkspaceHost } from '@/components/layout/WorkspaceHost'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'
import type { WorkspaceTab } from '@/types/layout'

const stubRenderTabContent = () => <div />

function makeTab(id: string): WorkspaceTab {
  return { id, title: id, component: 'x' } as WorkspaceTab
}

describe('功能点：WorkspaceHost 渲染插件声明的工作区 widget', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('ui_schema 声明的 workspace widget 经 WorkspaceHost 出现在工作区 DOM', () => {
    const StatusCard: WidgetComponent = (props: { label?: string }) => (
      <div data-testid="status-card-widget">{props.label}</div>
    )
    widgetRegistry.register('status_card', StatusCard, {
      name: 'status_card',
      supportedSpaces: ['workspace'],
    })
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'cost-agent',
          name: 'Cost',
          version: '1',
          ui_schema: {
            widgets: [{ id: 'cost-widget', type: 'status_card', space: 'workspace', props: { label: '月度成本 ¥1280' } }],
          },
        },
      ],
    } as never)

    render(
      <WorkspaceHost
        tabs={[makeTab('t1')]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={stubRenderTabContent}
      />,
    )

    expect(screen.getByTestId('declared-widget-cost-widget')).toBeInTheDocument()
    expect(screen.getByText('月度成本 ¥1280')).toBeInTheDocument()
  })

  it('无声明 widget 时 WorkspaceHost 与原 WorkspacePanel 一致（不额外渲染）', () => {
    const { container } = render(
      <WorkspaceHost
        tabs={[makeTab('t1')]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={stubRenderTabContent}
      />,
    )
    expect(container.querySelector('[data-testid="declared-widget-layer"]')).toBeNull()
  })

  it('声明 chat 空间的 widget 不进入 workspace（空间隔离）', () => {
    widgetRegistry.register('w', (() => <div data-testid="w">ok</div>) as WidgetComponent, {
      name: 'w',
      supportedSpaces: ['workspace', 'chat'],
    })
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          name: 'P',
          version: '1',
          ui_schema: { widgets: [{ id: 'chat-only', type: 'w', space: 'chat' }] },
        },
      ],
    } as never)

    render(
      <WorkspaceHost
        tabs={[makeTab('t1')]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={stubRenderTabContent}
      />,
    )

    expect(screen.queryByTestId('declared-widget-chat-only')).not.toBeInTheDocument()
  })
})
