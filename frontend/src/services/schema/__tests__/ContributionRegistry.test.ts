/**
 * ContributionRegistry 数据层测试
 *
 * 覆盖 task_11 P1-6：聚合 /api/v1/schema 的插件能力（config_files + ui_schema），
 * 提供查询接口。本测试只验数据聚合/查询，不做任何渲染。
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { ContributionRegistry, contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { SchemaResponse } from '@/services/api/schema'

function makeSchema(overrides: Partial<SchemaResponse> = {}): SchemaResponse {
  return {
    agents: [],
    pipelines: [],
    tools: [],
    routes: {},
    ...overrides,
  } as SchemaResponse
}

describe('ContributionRegistry — 插件配置聚合', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('loadFromSchema 后能按 pluginId 取到配置文件列表', () => {
    const schema = makeSchema({
      plugin_configs: [
        {
          plugin_id: 'connectors',
          plugin_name: '连接器',
          config_files: [
            { id: 'godot', path: 'config/external_tools/godot.yaml', label: 'Godot' },
            { id: 'vscode', path: 'config/external_tools/vscode.yaml', label: 'VSCode' },
          ],
        },
      ],
    })

    registry.loadFromSchema(schema)

    const files = registry.getPluginConfigFiles('connectors')
    expect(files).toHaveLength(2)
    expect(files[0].id).toBe('godot')
    expect(files[1].label).toBe('VSCode')
  })

  it('未知 pluginId 取配置文件返回空数组', () => {
    registry.loadFromSchema(makeSchema())
    expect(registry.getPluginConfigFiles('nope')).toEqual([])
  })

  it('getPluginConfigs 返回全部插件配置条目', () => {
    const schema = makeSchema({
      plugin_configs: [
        { plugin_id: 'a', plugin_name: 'A', config_files: [{ id: 'f', path: 'p', label: 'L' }] },
        { plugin_id: 'b', plugin_name: 'B', config_files: [] },
      ],
    })
    registry.loadFromSchema(schema)

    const entries = registry.getPluginConfigs()
    expect(entries.map((e) => e.pluginId)).toEqual(['a', 'b'])
  })

  it('schema 无 plugin_configs 时所有查询返回空', () => {
    registry.loadFromSchema(makeSchema())
    expect(registry.getPluginConfigs()).toEqual([])
    expect(registry.getPluginConfigFiles('any')).toEqual([])
  })

  it('hasPluginConfig 区分是否声明配置', () => {
    registry.loadFromSchema(
      makeSchema({
        plugin_configs: [
          { plugin_id: 'a', plugin_name: 'A', config_files: [{ id: 'f', path: 'p', label: 'L' }] },
        ],
      }),
    )
    expect(registry.hasPluginConfig('a')).toBe(true)
    expect(registry.hasPluginConfig('b')).toBe(false)
  })
})

describe('ContributionRegistry — UI Widget（ui_schema）聚合', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 agents 的 ui_schema 提取 widget 声明', () => {
    const schema = makeSchema({
      agents: [
        {
          id: 'review',
          name: 'Review Agent',
          version: '1.0',
          ui_schema: {
            widgets: [
              { id: 'review_panel', type: 'review_document', space: 'workspace', trigger: 'on_route_signal:wait' },
            ],
          },
        },
      ],
    })

    registry.loadFromSchema(schema)

    const widgets = registry.getWidgetsForPlugin('review')
    expect(widgets).toHaveLength(1)
    expect(widgets[0].type).toBe('review_document')
  })

  it('从 pipelines 的 ui_schema 提取 widget 声明', () => {
    const schema = makeSchema({
      pipelines: [
        {
          id: 'cost',
          name: 'Cost Pipeline',
          version: '1.0',
          role: 'cost',
          ui_schema: { widgets: [{ id: 'cost_chart', type: 'chart', space: 'workspace' }] },
        },
      ],
    })

    registry.loadFromSchema(schema)

    const widgets = registry.getWidgetsForPlugin('cost')
    expect(widgets).toHaveLength(1)
    expect(widgets[0].id).toBe('cost_chart')
  })

  it('ui_schema 缺失的插件返回空 widget 列表', () => {
    registry.loadFromSchema(
      makeSchema({
        agents: [{ id: 'plain', name: 'Plain', version: '1.0', ui_schema: null }],
      }),
    )
    expect(registry.getWidgetsForPlugin('plain')).toEqual([])
  })

  it('getWidgetsForPlugin 对未声明 ui_schema 的插件返回空数组', () => {
    registry.loadFromSchema(makeSchema())
    expect(registry.getWidgetsForPlugin('missing')).toEqual([])
  })

  it('getAllWidgets 聚合所有插件的 widget 声明', () => {
    registry.loadFromSchema(
      makeSchema({
        agents: [
          { id: 'a', name: 'A', version: '1', ui_schema: { widgets: [{ id: 'w1', type: 't1' }] } },
        ],
        pipelines: [
          { id: 'p', name: 'P', version: '1', role: 'r', ui_schema: { widgets: [{ id: 'w2', type: 't2' }] } },
        ],
      }),
    )

    const all = registry.getAllWidgets()
    expect(all.map((w) => w.id)).toEqual(['w1', 'w2'])
  })

  it('getAllWidgets 携带来源 pluginId', () => {
    registry.loadFromSchema(
      makeSchema({
        agents: [
          { id: 'a', name: 'A', version: '1', ui_schema: { widgets: [{ id: 'w1', type: 't1' }] } },
        ],
      }),
    )

    const all = registry.getAllWidgets()
    expect(all[0].pluginId).toBe('a')
  })
})

describe('ContributionRegistry — 重新加载与单例', () => {
  it('再次 loadFromSchema 覆盖旧数据（幂等重载）', () => {
    const registry = new ContributionRegistry()
    registry.loadFromSchema(
      makeSchema({
        plugin_configs: [
          { plugin_id: 'a', plugin_name: 'A', config_files: [{ id: 'f', path: 'p', label: 'L' }] },
        ],
      }),
    )
    expect(registry.getPluginConfigs()).toHaveLength(1)

    registry.loadFromSchema(makeSchema())
    expect(registry.getPluginConfigs()).toEqual([])
  })

  it('contributionRegistry 单例导出可用', () => {
    expect(contributionRegistry).toBeInstanceOf(ContributionRegistry)
  })
})
