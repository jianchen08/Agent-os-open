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

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { contributionRegistry, type PageDeclaration } from '@/services/schema/ContributionRegistry'
import { useContextKeys } from '@/stores/contextKeysStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
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

describe('PluginStatusItems — onClick 导航行为（模式体系 §5.0 通用能力）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useWidgetEventStore.setState({ events: {}, latest: {} })
    useContextKeys.setState({ keys: {} })
    useLayoutModeStore.setState({ workspaceTabs: [] })
    useNotificationStore.setState({ notifications: [] })
  })

  function registerStatusBarItem(overrides: Partial<PageDeclaration>): void {
    // 模拟 contributes.statusBarItems 归一化产物（legacyFrom 标记来源）
    contributionRegistry.register({
      type: 'statusBarItems',
      id: overrides.id ?? 'sb1',
      title: '状态项',
      space: 'dock',
      slot: 'status',
      ...overrides,
    } as PageDeclaration)
  }

  function registerTargetPage(id: string, pluginId?: string): void {
    contributionRegistry.register({
      type: 'pages',
      id,
      title: `页面 ${id}`,
      space: 'workspace',
      widget: 'webview',
      pluginId,
    })
  }

  it('contributes.statusBarItems 声明即现：icon 渲染为图标，非 navigate 声明为纯展示', () => {
    registerStatusBarItem({ id: 'sb-icon', title: '成本', icon: '💰' })
    registerStatusBarItem({
      id: 'sb-static',
      title: '只读',
      onClick: { type: 'focus', target: 'somewhere' },
    })

    render(<PluginStatusItems />)

    expect(screen.getByText('💰')).toBeInTheDocument()
    expect(screen.getByText('成本')).toBeInTheDocument()
    // 未知行为类型 = 纯展示（无按钮语义）
    expect(screen.queryByTestId('status-item-nav-sb-static')).not.toBeInTheDocument()
  })

  it('点击 navigate 条目 → openPluginPage 打开目标页工作区页签（声明者插件名下解析）', () => {
    registerTargetPage('delivery_panel', 'mode_demo')
    registerStatusBarItem({
      id: 'sb-nav',
      title: '交付台',
      pluginId: 'mode_demo',
      onClick: { type: 'navigate', page: 'delivery_panel' },
    })

    render(<PluginStatusItems />)
    fireEvent.click(screen.getByTestId('status-item-nav-sb-nav'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    expect(tabs[0].id).toBe('ws-plugin-delivery_panel')
  })

  it('跨插件同名页不串页：优先声明者插件名下的 page id', () => {
    registerTargetPage('panel_x', 'plugin_a')
    registerTargetPage('panel_x', 'plugin_b')
    registerStatusBarItem({
      id: 'sb-nav2',
      title: 'A 面板',
      pluginId: 'plugin_b',
      onClick: { type: 'navigate', page: 'panel_x' },
    })

    render(<PluginStatusItems />)
    fireEvent.click(screen.getByTestId('status-item-nav-sb-nav2'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs).toHaveLength(1)
    // 声明者 plugin_b 名下的 panel_x（moduleId 标来源插件）
    expect(tabs[0].moduleId).toBe('__plugin_plugin_b__')
  })

  it('目标页不存在 → 错误通知、不开页签（禁用插件同源失效显式可见）', () => {
    registerStatusBarItem({
      id: 'sb-nav3',
      title: '悬空项',
      pluginId: 'mode_gone',
      onClick: { type: 'navigate', page: 'missing_page' },
    })

    render(<PluginStatusItems />)
    fireEvent.click(screen.getByTestId('status-item-nav-sb-nav3'))

    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
    const notifications = useNotificationStore.getState().notifications
    expect(notifications.some((n) => n.message.includes('missing_page'))).toBe(true)
  })
})
