/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * PluginPagesHubPanel — 插件页面导航面板测试
 *
 * 核验行为（真实 contributionRegistry + 真实 openPluginPage 链路）：
 * - getPages() 全量按 space 分组渲染（条目 = 图标 + 标题 + 来源插件）
 * - 搜索过滤条目（标题/插件来源）；无命中显式空态
 * - 空间分组可折叠/展开
 * - 点击条目：path 声明走工作区页签直达；widget 声明直开；无渲染目标显式报错不静默
 * - registry 空 → 显式占位
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { PluginPagesHubPanel } from '../PluginPagesHubPanel'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 批量播种插件页面声明（loadFromSchema 幂等重载，等价 /api/v1/schema 装载） */
function seedPages(
  pages: Array<Record<string, unknown>>,
  pluginId = 'mode_coding',
): void {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [{ plugin_id: pluginId, plugin_name: 'Plugin', contributes: { pages } }],
    plugin_configs: [],
  })
}

/** 取工作区当前页签集合（openPluginPage 打开结果的观察面） */
function currentTabs() {
  return useLayoutModeStore.getState().workspaceTabs
}

beforeEach(() => {
  contributionRegistry.clear()
  useLayoutModeStore.setState({ workspaceTabs: [] })
  useNotificationStore.setState({ notifications: [] })
})
afterEach(() => {
  contributionRegistry.clear()
})

describe('PluginPagesHubPanel — 按 space 分组展示', () => {
  it('getPages 全量按空间分组渲染，条目带图标/标题/来源插件', () => {
    seedPages([
      { id: 'coding_delivery', title: '编码交付', icon: '💻', space: 'workspace', slot: 'tab', widget: 'coding_delivery_panel' },
      { id: 'llm_settings', title: '模型设置', icon: '⚙️', space: 'settings', slot: 'nav' },
      { id: 'chat_cmd', title: '聊天动作', icon: '💬', space: 'chat', slot: 'input-action' },
      { id: 'float_panel', title: '浮窗页', icon: '🪟', space: 'floating', slot: 'panel' },
    ])
    render(<PluginPagesHubPanel />)

    expect(screen.getByTestId('hub-group-workspace')).toBeInTheDocument()
    expect(screen.getByTestId('hub-group-settings')).toBeInTheDocument()
    expect(screen.getByTestId('hub-group-chat')).toBeInTheDocument()
    expect(screen.getByTestId('hub-group-floating')).toBeInTheDocument()
    // 条目：标题 + 来源插件同卡呈现（数据测试 id 用 pluginId:id 复合键，跨插件撞 id 不串）
    expect(screen.getByTestId('hub-item-mode_coding:coding_delivery')).toHaveTextContent('编码交付')
    expect(screen.getByTestId('hub-item-mode_coding:coding_delivery')).toHaveTextContent('mode_coding')
    expect(screen.getByTestId('hub-item-mode_coding:llm_settings')).toHaveTextContent('⚙️')
  })

  it('registry 为空 → 显式占位（不静默空白）', () => {
    render(<PluginPagesHubPanel />)
    expect(screen.getByTestId('hub-empty')).toHaveTextContent(/暂无插件页面声明/)
  })

  it('搜索命中标题 → 只保留命中条目与所在分组', () => {
    seedPages([
      { id: 'coding_delivery', title: '编码交付', space: 'workspace', slot: 'tab', widget: 'coding_delivery_panel' },
      { id: 'writing_workshop', title: '写作工坊', space: 'workspace', slot: 'tab', widget: 'writing_workshop_panel' },
    ])
    render(<PluginPagesHubPanel />)
    fireEvent.change(screen.getByTestId('hub-search'), { target: { value: '写作' } })

    expect(screen.getByTestId('hub-item-mode_coding:writing_workshop')).toBeInTheDocument()
    expect(screen.queryByTestId('hub-item-mode_coding:coding_delivery')).not.toBeInTheDocument()
  })

  it('搜索按来源插件过滤（插件名命中其全部页面）', () => {
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_writing',
          plugin_name: 'mode_writing',
          contributes: {
            pages: [{ id: 'writing_workshop', title: '写作工坊', space: 'workspace', slot: 'tab', widget: 'writing_workshop_panel' }],
          },
        },
        {
          plugin_id: 'debug_center',
          plugin_name: 'debug_center',
          contributes: {
            pages: [{ id: 'tasks', title: '任务', space: 'workspace', slot: 'tab', widget: 'debug_tasks' }],
          },
        },
      ],
      plugin_configs: [],
    })
    render(<PluginPagesHubPanel />)
    fireEvent.change(screen.getByTestId('hub-search'), { target: { value: 'mode_writing' } })

    expect(screen.getByTestId('hub-item-mode_writing:writing_workshop')).toBeInTheDocument()
    expect(screen.queryByTestId('hub-item-debug_center:tasks')).not.toBeInTheDocument()
  })

  it('搜索无命中 → 显式空态（区分于 registry 空占位）', () => {
    seedPages([{ id: 'p1', title: '页面一', space: 'workspace', slot: 'tab', widget: 'w1' }])
    render(<PluginPagesHubPanel />)
    fireEvent.change(screen.getByTestId('hub-search'), { target: { value: '不存在的词' } })

    expect(screen.getByTestId('hub-empty')).toHaveTextContent(/没有匹配/)
  })

  it('空间分组折叠/展开（aria-expanded 同步）', () => {
    seedPages([{ id: 'coding_delivery', title: '编码交付', space: 'workspace', slot: 'tab', widget: 'coding_delivery_panel' }])
    render(<PluginPagesHubPanel />)

    expect(screen.getByTestId('hub-group-workspace')).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByTestId('hub-group-workspace'))
    expect(screen.getByTestId('hub-group-workspace')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByTestId('hub-item-mode_coding:coding_delivery')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('hub-group-workspace'))
    expect(screen.getByTestId('hub-item-mode_coding:coding_delivery')).toBeInTheDocument()
  })
})

describe('PluginPagesHubPanel — 点击条目打开页面', () => {
  it('path 声明 → 经 openWorkspacePanelByPath 打开工作区页签（声明映射透传）', () => {
    seedPages([
      {
        id: 'coding_delivery',
        title: '编码交付',
        icon: '💻',
        space: 'workspace',
        slot: 'tab',
        widget: 'coding_delivery_panel',
        path: '/p/coding_delivery',
        datasourceUri: 'http_endpoints://tasks/list',
        props: { foo: 'bar' },
      },
    ])
    render(<PluginPagesHubPanel />)
    fireEvent.click(screen.getByTestId('hub-item-mode_coding:coding_delivery'))

    const tab = currentTabs().find((t) => t.id === 'ws-plugin-coding_delivery')
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('coding_delivery_panel')
    expect(tab?.moduleId).toBe('__plugin_mode_coding__')
    expect(tab?.dataSource).toBe('http_endpoints://tasks/list')
    expect(tab?.props).toEqual({ foo: 'bar' })
  })

  it('widget 声明（无 path）→ 按声明直开工作区页签', () => {
    seedPages([
      { id: 'hub_self', title: '插件页面', space: 'workspace', slot: 'tab', widget: 'plugin_pages_hub_panel' },
    ])
    render(<PluginPagesHubPanel />)
    fireEvent.click(screen.getByTestId('hub-item-mode_coding:hub_self'))

    const tab = currentTabs().find((t) => t.id === 'ws-plugin-hub_self')
    expect(tab?.component).toBe('plugin_pages_hub_panel')
  })

  it('无 path 且无 widget → 显式 error 通知，不开页签（不静默）', () => {
    seedPages([
      { id: 'config_page', title: '配置页', space: 'settings', slot: 'nav' },
    ])
    render(<PluginPagesHubPanel />)
    fireEvent.click(screen.getByTestId('hub-item-mode_coding:config_page'))

    expect(currentTabs()).toHaveLength(0)
    const notifications = useNotificationStore.getState().notifications
    expect(notifications.some((n) => n.category === 'error' && n.title === '页面无法打开')).toBe(true)
  })

  it('重复点击同一条目 → 激活既有页签，不重复追加', () => {
    seedPages([
      { id: 'coding_delivery', title: '编码交付', space: 'workspace', slot: 'tab', widget: 'coding_delivery_panel', path: '/p/coding_delivery' },
    ])
    render(<PluginPagesHubPanel />)
    fireEvent.click(screen.getByTestId('hub-item-mode_coding:coding_delivery'))
    fireEvent.click(screen.getByTestId('hub-item-mode_coding:coding_delivery'))

    expect(currentTabs().filter((t) => t.id === 'ws-plugin-coding_delivery')).toHaveLength(1)
  })
})
