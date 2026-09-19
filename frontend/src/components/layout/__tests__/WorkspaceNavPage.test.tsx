/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * WorkspaceNavPage —— 统一导航页测试
 *
 * 契约：
 * - 内容完全由 ContributionRegistry 聚合的页面声明驱动，无硬编码页面清单：
 *   主体 = workspace 空间大卡片分组（模式面板组置顶 / 工作区页签组 / 活动栏组，
 *   空组不占位）；非 workspace 空间收进「更多」折叠区小卡片分组，默认折叠
 * - 顶部搜索框按标题/页面 id/来源插件跨空间过滤；搜索有值「更多」区自动展开，
 *   无命中显式空态（区分于 registry 空占位）
 * - 点击条目（大小卡片）经既有 openPluginPage 链路打开对应落点并激活
 * - 工作区标签区为空时渲染导航页，有页签时导航页不占位
 *
 * 测试策略：真实 ContributionRegistry / openPluginPage / layoutModeStore
 * （纯本地聚合与 store，无网络依赖，不 mock）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { WorkspaceNavPage } from '@/components/layout/WorkspaceNavPage'
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 声明播种：两个模式面板 + 两个普通页签 + 一个活动栏页 + 一个 settings 空间页（带 path） */
function seedPages(): void {
  const pages: PageDeclaration[] = [
    { type: 'pages', id: 'coding_delivery', title: '编码交付', icon: '💻', space: 'workspace', slot: 'tab', path: '/p/coding_delivery', mode: 'coding', pluginId: 'mode_coding' },
    { type: 'pages', id: 'writing_workshop', title: '写作工坊', icon: '✍️', space: 'workspace', slot: 'tab', path: '/p/writing_workshop', mode: 'writing', pluginId: 'mode_writing' },
    { type: 'pages', id: 'memory', title: '记忆管理', space: 'workspace', slot: 'tab', path: '/memory', pluginId: 'memory' },
    { type: 'pages', id: 'tasks', title: '任务管理', space: 'workspace', slot: 'tab', path: '/tasks', pluginId: 'task_service' },
    { type: 'pages', id: 'monitoring', title: '监控', space: 'workspace', slot: 'activity-bar', path: '/monitoring', pluginId: 'monitoring' },
    { type: 'pages', id: 'llm_settings', title: '模型设置', icon: '⚙️', space: 'settings', slot: 'nav', path: '/settings/llm', pluginId: 'llm_service' },
  ]
  for (const page of pages) contributionRegistry.register(page)
}

beforeEach(() => {
  contributionRegistry.clear()
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.setState({ notifications: [] })
})

describe('WorkspaceNavPage — 声明驱动分组', () => {
  it('主体按声明分组：模式面板组置顶/工作区页签组/活动栏组；非 workspace 空间入「更多」区默认折叠', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('nav-group-mode')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-tab')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-activity-bar')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-mode_coding:coding_delivery')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-memory:memory')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-monitoring:monitoring')).toBeInTheDocument()
    // 模式组置顶：DOM 顺序在页签组之前
    expect(
      screen.getByTestId('nav-group-mode').compareDocumentPosition(screen.getByTestId('nav-group-tab')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    // 非 workspace 空间默认折叠：toggle 在（计数 = 非 workspace 页面总数）但小卡片不可见
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('nav-more-toggle')).toHaveTextContent('更多')
    expect(screen.getByTestId('nav-more-toggle')).toHaveTextContent('1')
    expect(screen.queryByTestId('nav-item-llm_service:llm_settings')).not.toBeInTheDocument()
  })

  it('无模式面板声明时模式组不占位（空组不渲染）；无非 workspace 页面时「更多」区不占位', () => {
    contributionRegistry.register({
      type: 'pages', id: 'memory', title: '记忆管理',
      space: 'workspace', slot: 'tab', path: '/memory', pluginId: 'memory',
    })
    render(<WorkspaceNavPage />)
    expect(screen.queryByTestId('nav-group-mode')).toBeNull()
    expect(screen.getByTestId('nav-group-tab')).toBeInTheDocument()
    expect(screen.queryByTestId('nav-more')).toBeNull()
  })

  it('点击大卡片经 opener 打开对应工作区页签并激活（再点另一条目激活权转移）', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-item-mode_coding:coding_delivery'))
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-coding_delivery'])
    expect(tabs[0]?.isActive).toBe(true)

    fireEvent.click(screen.getByTestId('nav-item-memory:memory'))
    const after = useLayoutModeStore.getState().workspaceTabs
    expect(after.map((t) => t.id)).toEqual(['ws-plugin-coding_delivery', 'ws-plugin-memory'])
    expect(after.find((t) => t.id === 'ws-plugin-memory')?.isActive).toBe(true)
    expect(after.find((t) => t.id === 'ws-plugin-coding_delivery')?.isActive).toBe(false)
  })

  it('registry 为空时渲染空态提示不崩溃', () => {
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()
    expect(screen.getByTestId('nav-empty')).toBeInTheDocument()
  })
})

describe('WorkspaceNavPage — 搜索过滤', () => {
  it('搜索命中 workspace 组标题 → 只保留命中条目（不匹配的不占位）', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: '写作' } })

    expect(screen.getByTestId('nav-item-mode_writing:writing_workshop')).toBeInTheDocument()
    expect(screen.queryByTestId('nav-item-mode_coding:coding_delivery')).not.toBeInTheDocument()
    expect(screen.queryByTestId('nav-item-memory:memory')).not.toBeInTheDocument()
  })

  it('搜索按来源插件过滤（插件名命中其全部页面）', () => {
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_writing',
          plugin_name: 'mode_writing',
          contributes: {
            pages: [{ id: 'writing_workshop', title: '写作工坊', space: 'workspace', slot: 'tab', path: '/p/writing_workshop' }],
          },
        },
        {
          plugin_id: 'debug_center',
          plugin_name: 'debug_center',
          contributes: {
            pages: [{ id: 'tasks', title: '任务', space: 'workspace', slot: 'tab', path: '/tasks' }],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<WorkspaceNavPage />)
    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: 'mode_writing' } })

    expect(screen.getByTestId('nav-item-mode_writing:writing_workshop')).toBeInTheDocument()
    expect(screen.queryByTestId('nav-item-debug_center:tasks')).not.toBeInTheDocument()
  })

  it('搜索命中非 workspace 组 → 「更多」区自动展开且未命中的组不占位', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')

    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: '模型' } })

    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('nav-group-settings')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-llm_service:llm_settings')).toBeInTheDocument()
    // workspace 大卡片未命中不占位
    expect(screen.queryByTestId('nav-group-mode')).toBeNull()
    expect(screen.queryByTestId('nav-item-mode_coding:coding_delivery')).not.toBeInTheDocument()
  })

  it('搜索无命中 → 显式空态（区分于 registry 空占位）', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: '不存在的词' } })

    expect(screen.getByTestId('nav-no-match')).toHaveTextContent(/没有匹配/)
  })
})

describe('WorkspaceNavPage — 「更多」折叠区', () => {
  it('默认收起，点击展开/再点收起（aria-expanded 同步）', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.queryByTestId('nav-item-llm_service:llm_settings')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('nav-group-settings')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-llm_service:llm_settings')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('nav-item-llm_service:llm_settings')).not.toBeInTheDocument()
  })

  it('小卡片点击 → openPluginPage 打开对应页签（path 声明直达）；重复点击激活既有页签不重复追加', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    fireEvent.click(screen.getByTestId('nav-item-llm_service:llm_settings'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-llm_settings'])
    expect(tabs[0]?.component).toBe('llm_settings')

    fireEvent.click(screen.getByTestId('nav-item-llm_service:llm_settings'))
    expect(useLayoutModeStore.getState().workspaceTabs.filter((t) => t.id === 'ws-plugin-llm_settings')).toHaveLength(1)
  })
})

describe('WorkspacePanel — 导航页兜底占位', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('标签区为空时渲染导航页；有页签时导航页不占位', () => {
    const { rerender } = render(
      <WorkspacePanel tabs={[]} onTabChange={() => {}} onTabClose={() => {}} renderTabContent={() => null} />,
    )
    expect(screen.getByTestId('workspace-nav-page')).toBeInTheDocument()

    rerender(
      <WorkspacePanel
        tabs={[{ id: 't1', title: '任务管理', isActive: true, isPinned: false, moduleId: 'm', component: 'x' } as never]}
        onTabChange={() => {}}
        onTabClose={() => {}}
        renderTabContent={() => null}
      />,
    )
    expect(screen.queryByTestId('workspace-nav-page')).toBeNull()
  })
})
