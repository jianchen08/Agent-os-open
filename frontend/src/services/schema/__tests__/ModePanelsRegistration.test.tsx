/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 模式面板注册契约测试（退役改判后：统一导航页 + webview 内建形态）
 *
 * 核验（模式体系落地设计 §2 末「面板承载形态」，2026-09-15 用户裁定；
 * 2026-09-18 统一导航页裁定：hub 宿主件退役，导航职责归 WorkspaceNavPage）：
 * 1. 四模式面板具名 widget 已退役——registry 不再注册（页面承载归各模式插件
 *    自带 webview 页，webview 是内建具名 widget，无需注册）。
 * 2. 插件页面 hub 宿主件已退役——registry 不再注册（防复活守护）。
 * 3. 装载含模式插件 webview 页声明 + 既有 activity-bar 声明的 schema 后，
 *    activity-bar 槽位页面数 == 声明基线（widget 注册零贡献）。
 * 4. webview 形态页面在统一导航页可见可点击（导航页消费 contributes.pages
 *    声明本身，与 widget 形态无关）；renderPageContent 按内建 webview 注册名解析。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { WorkspaceNavPage } from '@/components/layout/WorkspaceNavPage'
import { renderPageContent } from '@/components/schema/PageRenderer'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import type { ReactElement } from 'react'

/** 已退役的四模式面板具名 widget（前端不再注册，防复活守护） */
const RETIRED_PANEL_WIDGETS = [
  'coding_delivery_panel',
  'writing_workshop_panel',
  'roleplay_studio_panel',
  'research_desk_panel',
] as const

/** 模式插件 webview 页声明形态（= mode_coding manifest contributes.pages 条目） */
function webviewPage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return {
    type: 'pages',
    id: 'coding_delivery',
    title: '编码交付',
    icon: '💻',
    space: 'workspace',
    slot: 'tab',
    path: '/p/coding_delivery',
    widget: 'webview',
    pluginId: 'mode_coding',
    props: {
      pluginId: 'mode_coding',
      htmlPath: '/page/coding-panel',
      widgetId: 'coding_delivery',
    },
    ...overrides,
  }
}

beforeEach(() => {
  contributionRegistry.clear()
  useLayoutModeStore.setState({ workspaceTabs: [] })
  useNotificationStore.setState({ notifications: [] })
  initializeWidgets()
})

describe('模式面板退役 — 具名 widget 不再注册', () => {
  it('四个模式面板具名 widget 已从 registry 摘除（webview 内建承载，防复活）', () => {
    for (const name of RETIRED_PANEL_WIDGETS) {
      expect(widgetRegistry.getEntry(name), `widget ${name} 应已退役`).toBeUndefined()
    }
  })

  it('插件页面 hub 宿主件已退役（统一导航页吸收其职责，防复活）', () => {
    expect(widgetRegistry.getEntry('plugin_pages_hub_panel')).toBeUndefined()
  })
})

describe('模式面板退役 — activity-bar 零新增守护', () => {
  it('装载含模式 webview 页声明的 schema 后，activity-bar 槽位页面数 == 声明基线', () => {
    // 基线：既有 viewsContainers 归一化 1 项（activity-bar 槽位唯一来源）
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'agent_manager',
          plugin_name: 'Agent Manager',
          contributes: {
            viewsContainers: [{ id: 'agents', title: '会话', icon: '💬', path: '/' }],
          },
        },
        {
          plugin_id: 'mode_coding',
          plugin_name: 'Mode Coding',
          contributes: {
            pages: [
              webviewPage(),
              {
                type: 'pages',
                id: 'mode_selector_coding',
                title: '编码',
                icon: '💻',
                space: 'chat',
                slot: 'input-action',
                pluginId: 'mode_coding',
              } as PageDeclaration,
            ],
          },
        },
      ],
      plugin_configs: [],
    })

    const activityBarPages = contributionRegistry.getPages().filter((p) => p.slot === 'activity-bar')
    // 恰好 1 项 = viewsContainers 声明基线；模式页面声明未增加任何 activity-bar 页面
    expect(activityBarPages).toHaveLength(1)
    expect(activityBarPages[0].id).toBe('agents')
    // 模式页面按声明落 workspace tab 槽位（非 activity-bar）
    expect(contributionRegistry.getPage('coding_delivery')?.slot).toBe('tab')
  })
})

describe('模式面板退役 — webview 形态页面在统一导航页可见可点击', () => {
  it('导航页展示 webview 页声明条目（导航页消费声明本身，与 widget 形态无关）', () => {
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_coding',
          plugin_name: 'Mode Coding',
          contributes: { pages: [webviewPage()] },
        },
      ],
      plugin_configs: [],
    })
    render(<WorkspaceNavPage />)

    const item = screen.getByTestId('nav-item-mode_coding:coding_delivery')
    expect(item).toHaveTextContent('编码交付')
    expect(item).toHaveTextContent('💻')
  })

  it('点击 webview 页条目 → 打开工作区页签，component=webview 且 props 原样透传', () => {
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_coding',
          plugin_name: 'Mode Coding',
          contributes: { pages: [webviewPage()] },
        },
      ],
      plugin_configs: [],
    })
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-item-mode_coding:coding_delivery'))

    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-coding_delivery')
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('webview')
    expect(tab?.props).toEqual({
      pluginId: 'mode_coding',
      htmlPath: '/page/coding-panel',
      widgetId: 'coding_delivery',
    })
  })

  it('webview 页声明经内建注册名解析渲染（声明↔注册闭环，非降级替身）', () => {
    const element = renderPageContent(webviewPage()) as ReactElement
    expect(element.type).toBe(WebviewWidget)
  })
})
