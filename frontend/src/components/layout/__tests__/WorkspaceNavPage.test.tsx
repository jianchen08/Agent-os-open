/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * WorkspaceNavPage —— 统一导航页测试
 *
 * 契约：
 * - 内容完全由 ContributionRegistry 聚合的页面声明驱动，无硬编码页面清单：
 *   主体 = workspace 空间大卡片分组（模式面板组置顶 / 工作区页签组 / 活动栏组，
 *   空组不占位）+ 调试中心/设置一级直属分组（大卡片，不折叠）；其余非 workspace
 *   空间收进「更多」折叠区小卡片分组，默认折叠
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
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

/** 声明播种：两个模式面板 + 两个普通页签 + 一个活动栏页 + 调试中心/设置一级分组页 + 一个浮窗页（带 path+widget） */
function seedPages(): void {
  const pages: PageDeclaration[] = [
    { type: 'pages', id: 'coding_delivery', title: '编码交付', icon: '💻', space: 'workspace', slot: 'tab', path: '/p/coding_delivery', mode: 'coding', pluginId: 'mode_coding' },
    { type: 'pages', id: 'writing_workshop', title: '写作工坊', icon: '✍️', space: 'workspace', slot: 'tab', path: '/p/writing_workshop', mode: 'writing', pluginId: 'mode_writing' },
    { type: 'pages', id: 'memory', title: '记忆管理', space: 'workspace', slot: 'tab', path: '/memory', pluginId: 'memory' },
    { type: 'pages', id: 'tasks', title: '任务管理', space: 'workspace', slot: 'tab', path: '/tasks', pluginId: 'task_service' },
    { type: 'pages', id: 'monitoring', title: '监控', space: 'workspace', slot: 'activity-bar', path: '/monitoring', pluginId: 'monitoring' },
    { type: 'pages', id: 'debug_tasks', title: '调试任务', icon: '⚙️', space: 'debug_center', slot: 'tab', widget: 'debug_tasks', path: '/debug/tasks', pluginId: 'debug_center' },
    { type: 'pages', id: 'llm_settings', title: '模型设置', icon: '⚙️', space: 'settings', slot: 'nav', path: '/settings/llm', widget: 'llm_settings', pluginId: 'llm_service' },
    { type: 'pages', id: 'floating_notes', title: '浮窗笔记', icon: '🪟', space: 'floating', slot: 'panel', path: '/floating/notes', widget: 'notes_widget', pluginId: 'notes' },
  ]
  for (const page of pages) contributionRegistry.register(page)
}

beforeEach(() => {
  contributionRegistry.clear()
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.setState({ notifications: [] })
})

describe('WorkspaceNavPage — 声明驱动分组', () => {
  it('主体按声明分组：模式面板组置顶/工作区页签组/活动栏组；调试中心/设置为一级分组；其余非 workspace 空间入「更多」区默认折叠', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('nav-group-mode')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-tab')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-activity-bar')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-mode_coding:coding_delivery')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-memory:memory')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-monitoring:monitoring')).toBeInTheDocument()
    // 调试中心/设置为一级直属分组：大卡片直接可见，无需展开
    expect(screen.getByTestId('nav-group-debug_center')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-debug_center:debug_tasks')).toBeInTheDocument()
    expect(screen.getByTestId('nav-group-settings')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-llm_service:llm_settings')).toBeInTheDocument()
    // 模式组置顶：DOM 顺序在页签组之前
    expect(
      screen.getByTestId('nav-group-mode').compareDocumentPosition(screen.getByTestId('nav-group-tab')) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
    // 其余非 workspace 空间默认折叠：toggle 在（计数仅 floating 页）但小卡片不可见
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByTestId('nav-more-toggle')).toHaveTextContent('更多')
    expect(screen.getByTestId('nav-more-toggle')).toHaveTextContent('1')
    expect(screen.queryByTestId('nav-item-notes:floating_notes')).not.toBeInTheDocument()
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

  it('搜索命中一级直属分组（设置）→ 直接过滤展示（无需展开）；workspace 大卡片未命中不占位', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: '模型' } })

    expect(screen.getByTestId('nav-group-settings')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-llm_service:llm_settings')).toBeInTheDocument()
    // workspace 大卡片未命中不占位
    expect(screen.queryByTestId('nav-group-mode')).toBeNull()
    expect(screen.queryByTestId('nav-item-mode_coding:coding_delivery')).not.toBeInTheDocument()
  })

  it('搜索命中「更多」空间页 → 「更多」区自动展开且未命中的一级分组不占位', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')

    fireEvent.change(screen.getByTestId('nav-search'), { target: { value: '浮窗' } })

    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('nav-group-floating')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-notes:floating_notes')).toBeInTheDocument()
    // 一级直属分组未命中不占位
    expect(screen.queryByTestId('nav-group-settings')).toBeNull()
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
    expect(screen.queryByTestId('nav-item-notes:floating_notes')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByTestId('nav-group-floating')).toBeInTheDocument()
    expect(screen.getByTestId('nav-item-notes:floating_notes')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    expect(screen.getByTestId('nav-more-toggle')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('nav-item-notes:floating_notes')).not.toBeInTheDocument()
  })

  it('小卡片点击 → openPluginPage 打开对应页签（path 声明直达）；重复点击激活既有页签不重复追加', () => {
    seedPages()
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-more-toggle'))
    fireEvent.click(screen.getByTestId('nav-item-notes:floating_notes'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-floating_notes'])
    expect(tabs[0]?.component).toBe('notes_widget')

    fireEvent.click(screen.getByTestId('nav-item-notes:floating_notes'))
    expect(useLayoutModeStore.getState().workspaceTabs.filter((t) => t.id === 'ws-plugin-floating_notes')).toHaveLength(1)
  })

  it('config_files 配置声明页卡（无 path/widget）点击 → 打开设置中枢深链页签并激活，不落错误通知（BUG-77）', () => {
    // 生产形态：loadFromSchema 的 plugin_configs 归一化产物
    //（id=`{pluginId}:{fileId}`，datasourceUri=配置文件路径，无 path 无 widget）
    // 归一化落 settings 空间 → 导航页一级「设置」分组直接可见，无需展开
    contributionRegistry.loadFromSchema({
      plugin_configs: [
        {
          plugin_id: 'evaluation_service',
          plugin_name: 'Evaluation Service',
          config_files: [
            { id: 'evaluation_metrics', path: 'config/plugins/evaluation/evaluation_metrics.yaml', label: '评估指标定义' },
          ],
        },
      ],
    })
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-item-evaluation_service:evaluation_service:evaluation_metrics'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-config-evaluation_service-evaluation_metrics'])
    expect(tabs[0]?.isActive).toBe(true)
    expect(tabs[0]?.component).toBe('settings_hub')
    expect(tabs[0]?.props).toEqual({ initialActive: 'plugin:evaluation_service:evaluation_metrics' })
    // 断链契约：不得再落「页面无法打开」错误通知（旧实现点击零导航的表象）
    expect(
      useNotificationStore.getState().notifications.some((n) => n.category === 'error'),
    ).toBe(false)

    // 重复点击幂等：激活既有页签，不重复追加
    fireEvent.click(screen.getByTestId('nav-item-evaluation_service:evaluation_service:evaluation_metrics'))
    expect(
      useLayoutModeStore.getState().workspaceTabs.filter((t) => t.id === 'ws-plugin-config-evaluation_service-evaluation_metrics'),
    ).toHaveLength(1)
  })

  it('不同插件的配置声明页卡 → 各开独立深链页签（页签 id 与深链键随 pluginId:fileId 区分）', () => {
    contributionRegistry.loadFromSchema({
      plugin_configs: [
        {
          plugin_id: 'evaluation_service',
          plugin_name: 'Evaluation Service',
          config_files: [
            { id: 'evaluation_metrics', path: 'config/plugins/evaluation/evaluation_metrics.yaml', label: '评估指标定义' },
          ],
        },
        {
          plugin_id: 'approval',
          plugin_name: 'Approval Service',
          config_files: [
            { id: 'approval_rules', path: 'config/plugins/approval/approval_rules.yaml', label: '审批规则' },
          ],
        },
      ],
    })
    render(<WorkspaceNavPage />)
    fireEvent.click(screen.getByTestId('nav-item-approval:approval:approval_rules'))
    fireEvent.click(screen.getByTestId('nav-item-evaluation_service:evaluation_service:evaluation_metrics'))

    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual([
      'ws-plugin-config-approval-approval_rules',
      'ws-plugin-config-evaluation_service-evaluation_metrics',
    ])
    expect(tabs.find((t) => t.id === 'ws-plugin-config-approval-approval_rules')?.props).toEqual({
      initialActive: 'plugin:approval:approval_rules',
    })
    expect(tabs.find((t) => t.id === 'ws-plugin-config-evaluation_service-evaluation_metrics')?.isActive).toBe(true)
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
