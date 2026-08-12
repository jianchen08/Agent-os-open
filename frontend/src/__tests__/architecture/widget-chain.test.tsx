/**
 * 功能测试：§5.3 widget 声明→实现链路（架构愿景：插件声明的 widget 真渲染出来）
 *
 * 推演链：项目愿景（插件声明能力）→ 前端愿景（声明驱动渲染）→ 架构（getAllWidgets→
 * widgetRegistry→渲染 必须通电）→ 功能点（"声明的工作区 widget 在界面上出现"）。
 *
 * 本测试验证**功能点本身**：用真实 contributionRegistry 装载声明 + 真实 widgetRegistry
 * 注册实现 + 渲染真实 DeclaredWidgetLayer，断言 widget 内容出现在 DOM。
 * 不是源码字符串扫描，是端到端的行为验证。
 *
 * 关联：docs/working/design/frontend-design-unification-execution-plan.md §三 M0.1
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { resolveDeclaredWidgets } from '@/services/schema/widgetChain'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'

afterEach(() => {
  contributionRegistry.clear()
  widgetRegistry.clear()
})

describe('功能点：插件声明的 widget 经链路真渲染到 DOM', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('agents[].ui_schema.widgets 声明的 workspace widget 渲染其内容', () => {
    // 实现：注册 status_card 组件（渲染可见文本）
    const StatusCard: WidgetComponent = (props: { label?: string }) => (
      <div data-testid="status-card-widget">{props.label}</div>
    )
    widgetRegistry.register('status_card', StatusCard, {
      name: 'status_card',
      supportedSpaces: ['workspace'],
    })

    // 声明：插件在 ui_schema 声明一个 workspace widget（走真实 loadFromSchema）
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

    // 渲染真实消费组件（链路终点）
    render(
      <MemoryRouter>
        <DeclaredWidgetLayer space="workspace" />
      </MemoryRouter>,
    )

    // 断言：声明的 widget 内容真出现在 DOM（功能达成）
    expect(screen.getByTestId('declared-widget-cost-widget')).toBeInTheDocument()
    expect(screen.getByTestId('status-card-widget')).toBeInTheDocument()
    expect(screen.getByText('月度成本 ¥1280')).toBeInTheDocument()
  })

  it('声明 type 未注册实现时不渲染该 widget（断链可观测，不静默渲染空）', () => {
    contributionRegistry.loadFromSchema({
      agents: [
        {
          id: 'p',
          name: 'P',
          version: '1',
          ui_schema: { widgets: [{ id: 'orphan', type: 'no_such_impl', space: 'workspace' }] },
        },
      ],
    } as never)

    const { container } = render(
      <MemoryRouter>
        <DeclaredWidgetLayer space="workspace" />
      </MemoryRouter>,
    )

    // 无可渲染内容时返回 null，不渲染空容器
    expect(container.querySelector('[data-testid="declared-widget-layer"]')).toBeNull()
    expect(screen.queryByTestId('declared-widget-orphan')).not.toBeInTheDocument()
  })

  it('space 过滤：仅渲染匹配空间的声明（chat 声明不进 workspace）', () => {
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
          ui_schema: {
            widgets: [
              { id: 'ws-w', type: 'w', space: 'workspace' },
              { id: 'chat-w', type: 'w', space: 'chat' },
            ],
          },
        },
      ],
    } as never)

    render(
      <MemoryRouter>
        <DeclaredWidgetLayer space="workspace" />
      </MemoryRouter>,
    )

    expect(screen.getByTestId('declared-widget-ws-w')).toBeInTheDocument()
    expect(screen.queryByTestId('declared-widget-chat-w')).not.toBeInTheDocument()
  })
})

/**
 * 桥梁函数 resolveDeclaredWidgets 的单元行为（功能点：声明按 精确→降级→未解析 解析）
 * 这是链路的解析逻辑单元，配合上面的端到端功能测试共同覆盖。
 */
describe('resolveDeclaredWidgets 解析逻辑', () => {
  const fakeRegistry = (known: Record<string, WidgetComponent>) => ({
    get: (type: string) => known[type],
    findFallback: (type: string) => known[`__fallback_${type}`],
  })

  it('精确注册的 type 直接解析', () => {
    const Comp = (() => null) as WidgetComponent
    const { resolved, unresolved } = resolveDeclaredWidgets(
      [{ id: 'w', type: 'chart', pluginId: 'p' }],
      fakeRegistry({ chart: Comp }),
    )
    expect(resolved).toHaveLength(1)
    expect(resolved[0].viaFallback).toBe(false)
    expect(unresolved).toHaveLength(0)
  })

  it('未直接注册但有降级路径的 type 走 findFallback（viaFallback=true）', () => {
    const Fallback = (() => null) as WidgetComponent
    const { resolved } = resolveDeclaredWidgets(
      [{ id: 'w', type: 'kanban', pluginId: 'p' }],
      fakeRegistry({ __fallback_kanban: Fallback }),
    )
    expect(resolved).toHaveLength(1)
    expect(resolved[0].viaFallback).toBe(true)
  })

  it('完全未知的 type 进入 unresolved（不静默）', () => {
    const { resolved, unresolved } = resolveDeclaredWidgets(
      [{ id: 'w', type: 'ghost', pluginId: 'p' }],
      fakeRegistry({}),
    )
    expect(resolved).toHaveLength(0)
    expect(unresolved).toHaveLength(1)
    expect(unresolved[0].reason).toContain('ghost')
  })
})
