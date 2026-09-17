/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * WorkspaceNavPage —— 工作区导航页（标签区无已开页签时的兜底落点，R92 · D-2）
 *
 * 契约：
 * - 内容完全由 ContributionRegistry 聚合的 workspace 空间声明驱动，无硬编码页面
 *   清单；分组：slot=tab 带 mode 扩展字段 → 模式面板组；slot=tab 无 mode →
 *   工作区页签组；slot=activity-bar → 活动栏组；空组不占位；非 workspace 空间不出现
 * - 点击条目经既有 openPluginPage 链路打开对应工作区页签并激活
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

/** 声明播种：两个模式面板 + 两个普通页签 + 一个活动栏页 + 一个 settings 空间页 */
function seedPages(): void {
  const pages: PageDeclaration[] = [
    { type: 'pages', id: 'coding_delivery', title: '编码交付', icon: '💻', space: 'workspace', slot: 'tab', path: '/p/coding_delivery', mode: 'coding', pluginId: 'mode_coding' },
    { type: 'pages', id: 'writing_workshop', title: '写作工坊', icon: '✍️', space: 'workspace', slot: 'tab', path: '/p/writing_workshop', mode: 'writing', pluginId: 'mode_writing' },
    { type: 'pages', id: 'memory', title: '记忆管理', space: 'workspace', slot: 'tab', path: '/memory', pluginId: 'memory' },
    { type: 'pages', id: 'tasks', title: '任务管理', space: 'workspace', slot: 'tab', path: '/tasks', pluginId: 'task_service' },
    { type: 'pages', id: 'monitoring', title: '监控', space: 'workspace', slot: 'activity-bar', path: '/monitoring', pluginId: 'monitoring' },
    { type: 'pages', id: 'some_setting', title: '某配置', space: 'settings', slot: 'nav', pluginId: 'x' },
  ]
  for (const page of pages) contributionRegistry.register(page)
}

describe('WorkspaceNavPage — 声明驱动分组', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('按声明分组：模式面板组/工作区页签组/活动栏组，非 workspace 空间不出现', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('nav-group-mode')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-tab')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-activity-bar')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-mode_coding:coding_delivery')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-mode_writing:writing_workshop')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-memory:memory')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-monitoring:monitoring')).toBeInTheDocument()
    expect(screen.queryByText('某配置')).toBeNull()
  })

  it('无模式面板声明时模式组不占位（空组不渲染）', () => {
    contributionRegistry.register({
      type: 'pages', id: 'memory', title: '记忆管理',
      space: 'workspace', slot: 'tab', path: '/memory', pluginId: 'memory',
    })
    render(<WorkspaceNavPage />)
    expect(screen.queryByTestId('nav-group-mode')).toBeNull()
    expect(screen.getByTestId('nav-group-tab')).toBeInTheDocument()
  })

  it('点击条目经 opener 打开对应工作区页签并激活（再点另一条目激活权转移）', () => {
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
