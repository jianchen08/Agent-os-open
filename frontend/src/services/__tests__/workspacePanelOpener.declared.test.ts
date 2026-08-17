/**
 * 面板声明迁移测试（widget 化 T11）
 *
 * 验收口径：monitoring/hindsight_memory/cost_control 的面板入口由插件
 * contributes.pages 声明驱动——声明在 → openWorkspacePanelByPath 打开对应
 * widget 页签；声明移除（禁用插件）→ 不再命中（面板消失），不回退硬编码。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  openWorkspacePanelByPath,
  TOP_NAV_PANELS,
} from '@/services/workspacePanelOpener'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
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
            plugin_id: 'hindsight_memory',
            plugin_name: 'Hindsight Memory',
            contributes: {
              pages: [
                {
                  id: 'memory',
                  title: '记忆',
                  icon: '🧠',
                  space: 'workspace',
                  slot: 'activity-bar',
                  order: 30,
                  path: '/memory',
                  widget: 'memory_panel',
                },
              ],
            },
          },
          {
            plugin_id: 'cost_control',
            plugin_name: 'Cost Control',
            contributes: {
              pages: [
                {
                  id: 'cost_dashboard',
                  title: '成本',
                  icon: '💰',
                  space: 'workspace',
                  slot: 'activity-bar',
                  order: 40,
                  path: '/cost',
                  widget: 'cost_dashboard',
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

  it('/memory → memory_panel；/cost → cost_dashboard', () => {
    expect(openWorkspacePanelByPath('/memory')).toBe(true)
    expect(openWorkspacePanelByPath('/cost')).toBe(true)
    const components = useLayoutModeStore
      .getState()
      .workspaceTabs.map((t) => t.component)
    expect(components).toContain('memory_panel')
    expect(components).toContain('cost_dashboard')
  })

  it('禁用插件（声明移除）→ 面板入口消失（不再命中硬编码）', () => {
    seedSchema('none')
    expect(openWorkspacePanelByPath('/monitoring')).toBe(false)
    expect(openWorkspacePanelByPath('/memory')).toBe(false)
    expect(openWorkspacePanelByPath('/cost')).toBe(false)
  })

  it('TOP_NAV_PANELS 不再持有监控/记忆条目（硬编码已摘除）', () => {
    expect(TOP_NAV_PANELS['/monitoring']).toBeUndefined()
    expect(TOP_NAV_PANELS['/memory']).toBeUndefined()
    // 内核自持项保留（T13 拍板范围，不在 T11 动）
    expect(TOP_NAV_PANELS['/settings']).toBeDefined()
    expect(TOP_NAV_PANELS['/tasks']).toBeDefined()
  })
})
