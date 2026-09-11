/**
 * 面板声明迁移测试（widget 化 T11）
 *
 * 验收口径：monitoring/agent_manager/task_service/user_admin 的面板入口由插件
 * contributes.pages 声明驱动——声明在 → openWorkspacePanelByPath 打开对应
 * widget 页签；声明移除（禁用插件）→ 不再命中（面板消失），不回退硬编码。
 * 样例声明为机制验证用合成数据（memory_panel 注册已随 P0-3 摘除，不再入样）。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import {
  openWorkspacePanelByPath,
  TOP_NAV_PANELS,
} from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 以三插件的真实声明形态装载 schema（等价 GrowthLoop → loadFromSchema） */
function seedSchema(enabled: 'all' | 'none') {
  const plugin_contributes =
    enabled === 'all'
      ? [
          {
            plugin_id: 'monitoring',
            plugin_name: 'Monitoring',
            contributes: {
              pages: [
                {
                  id: 'monitoring',
                  title: '监控',
                  icon: '📊',
                  space: 'workspace',
                  slot: 'activity-bar',
                  order: 20,
                  path: '/monitoring',
                  widget: 'monitoring_panel',
                },
              ],
            },
          },
          {
            plugin_id: 'agent_manager',
            plugin_name: 'Agent Manager',
            contributes: {
              pages: [
                {
                  id: 'agent_manager_agents',
                  title: '智能体',
                  icon: 'person',
                  space: 'workspace',
                  slot: 'activity-bar',
                  order: 30,
                  path: '/agents',
                  widget: 'agents_panel',
                },
              ],
            },
          },
          {
            plugin_id: 'task_service',
            plugin_name: 'Task Service',
            contributes: {
              pages: [
                {
                  id: 'tasks',
                  title: '任务管理',
                  icon: 'folder',
                  space: 'workspace',
                  slot: 'tab',
                  path: '/tasks',
                  widget: 'pipeline_manager',
                },
              ],
            },
          },
          {
            plugin_id: 'user_admin',
            plugin_name: 'User Admin',
            contributes: {
              pages: [
                {
                  id: 'admin',
                  title: '用户管理',
                  icon: '👥',
                  space: 'workspace',
                  slot: 'activity-bar',
                  path: '/admin',
                  widget: 'widget_stage',
                  props: { space: 'admin' },
                },
              ],
            },
          },
        ]
      : []

  contributionRegistry.loadFromSchema({
    plugin_contributes,
    plugin_configs: [],
  })
}

beforeEach(() => {
  useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null })
  seedSchema('all')
})

describe('T11：面板入口声明驱动', () => {
  it('/monitoring → 声明命中，打开 monitoring_panel 页签', () => {
    expect(openWorkspacePanelByPath('/monitoring')).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) =>
      t.id.startsWith('ws-plugin-monitoring'),
    )
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('monitoring_panel')
  })

  it('禁用插件（声明移除）→ 面板入口消失（不再命中硬编码）', () => {
    seedSchema('none')
    expect(openWorkspacePanelByPath('/monitoring')).toBe(false)
  })

  it('TOP_NAV_PANELS 不再持有监控/记忆/任务条目（硬编码已摘除）', () => {
    expect(TOP_NAV_PANELS['/monitoring']).toBeUndefined()
    expect(TOP_NAV_PANELS['/memory']).toBeUndefined()
    expect(TOP_NAV_PANELS['/tasks']).toBeUndefined()
    expect(TOP_NAV_PANELS['/settings/plugins']).toBeUndefined()
    // 内核自持项保留（设置中枢是壳 UI，T13 拍板范围）
    expect(TOP_NAV_PANELS['/settings']).toBeDefined()
  })

  it('task_service 声明化：/tasks 由插件 pages 声明（widget=pipeline_manager）', () => {
    expect(openWorkspacePanelByPath('/tasks')).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) =>
      t.id.startsWith('ws-plugin-tasks'),
    )
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('pipeline_manager')
    expect(tab?.moduleId).toBe('__plugin_task_service__')
  })

  it('task_service 禁用（声明移除）→ /tasks 不再命中（不回退硬编码）', () => {
    seedSchema('none')
    expect(openWorkspacePanelByPath('/tasks')).toBe(false)
  })

  it('user_admin 声明化：/admin → widget_stage 组台页（props.space 透传页签）', () => {
    expect(openWorkspacePanelByPath('/admin')).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) =>
      t.id.startsWith('ws-plugin-admin'),
    )
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('widget_stage')
    expect(tab?.props).toEqual({ space: 'admin' })
  })

  it('user_admin 禁用（声明移除）→ /admin 不再命中', () => {
    seedSchema('none')
    expect(openWorkspacePanelByPath('/admin')).toBe(false)
  })

  it('agent_manager 声明化（2026-08-20）：/agents 由插件 pages 声明，硬编码已摘除', () => {
    // agent_manager 插件 contributes.pages 声明 path=/agents → 命中 agents_panel
    expect(openWorkspacePanelByPath('/agents')).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) =>
      t.id.startsWith('ws-plugin-agent_manager'),
    )
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('agents_panel')
    // 硬编码 TOP_NAV_PANELS/工具页退役：条目不再存在
    expect(TOP_NAV_PANELS['/agents']).toBeUndefined()
    expect(TOP_NAV_PANELS['/tools']).toBeUndefined()
  })

  it('agent_manager 禁用（声明移除）→ /agents 不再命中（不回退硬编码）', () => {
    seedSchema('none')
    expect(openWorkspacePanelByPath('/agents')).toBe(false)
  })
})
