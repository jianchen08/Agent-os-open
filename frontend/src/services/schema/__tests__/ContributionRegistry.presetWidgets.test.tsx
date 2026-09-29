// @feature: FP-0.2.四 前端Schema(contributions 预置 widget) | @ci: frontend-test
/**
 * 前端预置 widget 声明通道测试（监控页「插件」tab 进程观测视图）
 *
 * 核验：registerPresetWidgets 与后端 ui_schema 声明同源聚合（getAllWidgets），
 * 同 id 后注册者覆盖，schema 幂等重载不清除预置声明；plugin_hosts 预置声明
 * 挂在监控页**现有「插件」组**且为该组唯一渲染——用户两项裁定：①宿主盒子
 * 就是插件，独立「宿主盒子」组不存在；②「插件运行」表与卡片是同一数据，
 * 不允许并存（plugins_runtime_table 声明经前端下架过滤摘除，列数据迁入卡片）。
 */

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ContributionRegistry, contributionRegistry } from '../ContributionRegistry'
import { WidgetStage } from '@/components/schema/widgets/WidgetStage'
import { initializeWidgets } from '../registerWidgets'
import { renderWithProviders } from '@/test/renderWithProviders'

// 组台集成用例挂载 PluginHostsWidget：mock 外部 API 请求层，数据面非本文件关注点
vi.mock('@/services/api/pluginHosts', () => ({
  getPluginHosts: vi.fn(async () => ({ hosts: [], pending_spawn: [] })),
  getPluginRuntimeRows: vi.fn(async () => []),
}))

describe('ContributionRegistry — 前端预置 widget 声明', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('预置声明进 getAllWidgets，与插件 ui_schema 声明同源聚合', () => {
    registry.registerPresetWidgets([
      { id: 'plugin_hosts', type: 'plugin_hosts', space: 'monitoring', group: '插件', order: 55 },
    ])
    registry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'monitoring',
          plugin_name: '监控',
          ui_schema: { widgets: [{ id: 'tasks_table', type: 'table', space: 'monitoring', group: '任务' }] },
        },
      ],
    })

    const ids = registry.getAllWidgets().map((w) => w.id)
    expect(ids).toContain('plugin_hosts')
    expect(ids).toContain('tasks_table')
  })

  it('同 id 后注册者覆盖，不产生重复声明', () => {
    registry.registerPresetWidgets([{ id: 'x', type: 'table', order: 1 }])
    registry.registerPresetWidgets([{ id: 'x', type: 'chart', order: 2 }])
    const declared = registry.getAllWidgets().filter((w) => w.id === 'x')
    expect(declared).toHaveLength(1)
    expect(declared[0]?.type).toBe('chart')
  })

  it('loadFromSchema 幂等重载不清除预置声明（前端静态配置，非后端数据）', () => {
    registry.registerPresetWidgets([
      { id: 'plugin_hosts', type: 'plugin_hosts', space: 'monitoring' },
    ])
    registry.loadFromSchema({})
    registry.loadFromSchema({ plugin_contributes: [] })
    expect(registry.getAllWidgets().map((w) => w.id)).toContain('plugin_hosts')
  })

  it('未注册预置的全新实例 getAllWidgets 为空（预置不凭空出现）', () => {
    registry.loadFromSchema({})
    expect(registry.getAllWidgets()).toEqual([])
  })
})

describe('plugin_hosts 预置声明 — 挂监控页现有「插件」组（运行表声明已下架）', () => {
  beforeEach(() => {
    // 复刻监控插件 ui_schema 现状：plugins_runtime_table 在清单里（前端下架过滤负责摘除）
    contributionRegistry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'monitoring',
          plugin_name: '监控',
          ui_schema: {
            widgets: [
              {
                id: 'plugins_runtime_table',
                type: 'table',
                space: 'monitoring',
                group: '插件',
                order: 60,
                props: { title: '插件运行', data: [] },
              },
              { id: 'tasks_table', type: 'table', space: 'monitoring', group: '任务', order: 50 },
            ],
          },
        },
      ],
    })
    initializeWidgets()
  })

  it('「插件运行」表声明被前端下架（同一数据不并存），其余声明不受牵连', () => {
    const ids = contributionRegistry.getAllWidgets().map((w) => w.id)
    expect(ids).not.toContain('plugins_runtime_table')
    expect(ids).toContain('tasks_table')
  })

  it('plugin_hosts 预置声明 group=插件，且为「插件」组唯一成员（独立「宿主盒子」组不存在）', () => {
    const decl = contributionRegistry.getAllWidgets().find((w) => w.id === 'plugin_hosts')
    expect(decl).toBeDefined()
    expect(decl?.type).toBe('plugin_hosts')
    expect(decl?.space).toBe('monitoring')
    expect(decl?.group).toBe('插件')
    expect(decl?.order).toBe(55)
    const groupMembers = contributionRegistry
      .getAllWidgets()
      .filter((w) => w.space === 'monitoring' && w.group === '插件')
    expect(groupMembers.map((w) => w.id)).toEqual(['plugin_hosts'])
    expect(contributionRegistry.getAllWidgets().some((w) => w.group === '宿主盒子')).toBe(false)
  })

  it('WidgetStage 监控空间无「宿主盒子」独立 tab，「插件」tab 只渲染进程观测卡片', () => {
    renderWithProviders(<WidgetStage space="monitoring" />)
    expect(screen.queryByTestId('widget-stage-tab-宿主盒子')).not.toBeInTheDocument()
    expect(screen.getByTestId('widget-stage-tab-插件')).toBeInTheDocument()
    expect(screen.getByTestId('widget-stage-tab-任务')).toBeInTheDocument()

    fireEvent.click(screen.getByTestId('widget-stage-tab-插件'))
    expect(screen.getByTestId('plugin-hosts-widget')).toBeInTheDocument()
    expect(screen.queryByTestId('declared-widget-plugins_runtime_table')).not.toBeInTheDocument()
  })

  it('重复调用 initializeWidgets 幂等（预置声明不重复）', () => {
    initializeWidgets()
    const declared = contributionRegistry.getAllWidgets().filter((w) => w.id === 'plugin_hosts')
    expect(declared).toHaveLength(1)
  })
})
