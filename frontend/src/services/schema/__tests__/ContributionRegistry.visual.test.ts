/**
 * ContributionRegistry 视觉贡献（themes / client_styles）数据层测试
 *
 * 覆盖 task_plugin_frontend_customization.md 任务 1/2 的注册侧：
 * - contributes.themes / client_styles 走旁路注册表（不进 pages 归一化，无幽灵页面）
 * - 注册字段收窄（base 归一化 dark/light、scope 归一化 global/scoped）
 * - 幂等更新、clear 清理、查询接口
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
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

function contributesSchema(pluginId: string, contributes: Record<string, unknown[]>): SchemaResponse {
  return makeSchema({
    plugin_contributes: [{ plugin_id: pluginId, plugin_name: 'p', contributes }],
  })
}

describe('ContributionRegistry — contributes.themes（任务 1）', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('注册插件主题并提供查询', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        themes: [
          {
            id: 'gold-lace',
            name: '金色蕾丝',
            description: '测试',
            base: 'dark',
            variables: { '--ds-accent-primary': '#D4AF37' },
            backgrounds: { image: { enabled: false }, texture: { enabled: false } },
          },
        ],
      }),
    )

    const themes = registry.getPluginThemes()
    expect(themes).toHaveLength(1)
    expect(themes[0]).toMatchObject({
      id: 'gold-lace',
      name: '金色蕾丝',
      base: 'dark',
      pluginId: 'demo',
      variables: { '--ds-accent-primary': '#D4AF37' },
      backgrounds: { image: { enabled: false }, texture: { enabled: false } },
    })
    expect(registry.getPluginTheme('gold-lace')).toBe(themes[0])
    expect(registry.getThemesForPlugin('demo')).toHaveLength(1)
  })

  it('base 非 light 一律归一化为 dark（fail-safe 默认深色）', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        themes: [
          { id: 't1', name: 'T1', base: 'light' },
          { id: 't2', name: 'T2', base: 'dark' },
          { id: 't3', name: 'T3', base: 'solarized' }, // 未知 base → dark
          { id: 't4', name: 'T4' }, // 缺 base → dark
        ],
      }),
    )
    const byId = Object.fromEntries(registry.getPluginThemes().map((t) => [t.id, t.base]))
    expect(byId).toEqual({ t1: 'light', t2: 'dark', t3: 'dark', t4: 'dark' })
  })

  it('缺 id/name 的条目被丢弃（注册失败不静默渲染空壳）', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        themes: [
          { name: 'no-id' },
          { id: 'no-name' },
          { id: 'ok', name: 'OK' },
        ],
      }),
    )
    expect(registry.getPluginThemes()).toHaveLength(1)
    expect(registry.getPluginThemes()[0].id).toBe('ok')
  })

  it('同插件同名主题幂等更新（不重复追加）', () => {
    registry.loadFromSchema(
      contributesSchema('demo', { themes: [{ id: 't', name: 'V1' }] }),
    )
    registry.loadFromSchema(
      contributesSchema('demo', { themes: [{ id: 't', name: 'V2' }] }),
    )
    expect(registry.getPluginThemes()).toHaveLength(1)
    expect(registry.getPluginThemes()[0].name).toBe('V2')
  })

  it('跨插件同名 id 后注册者覆盖（冲突约定，风险 §四.1）', () => {
    registry.loadFromSchema(
      contributesSchema('a', { themes: [{ id: 't', name: 'A' }] }),
    )
    registry.loadFromSchema(
      contributesSchema('b', { themes: [{ id: 't', name: 'B' }] }),
    )
    expect(registry.getPluginTheme('t')?.name).toBe('B')
  })

  it('themes 不进 pages 归一化（无幽灵页面）', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        themes: [{ id: 'gold-lace', name: '金色蕾丝' }],
      }),
    )
    expect(registry.getPages()).toHaveLength(0)
    expect(registry.getPagesBySpace('workspace')).toHaveLength(0)
  })
})

describe('ContributionRegistry — contributes.client_styles（任务 2）', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('注册 CSS 声明并提供查询', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        client_styles: [
          {
            id: 'gold-lace-border',
            path: '/assets/border.css',
            scope: 'global',
            description: '金色蕾丝边框',
          },
        ],
      }),
    )

    const styles = registry.getClientStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0]).toMatchObject({
      id: 'gold-lace-border',
      path: '/assets/border.css',
      scope: 'global',
      pluginId: 'demo',
    })
    expect(registry.getClientStylesForPlugin('demo')).toHaveLength(1)
    expect(registry.getClientStylesForPlugin('other')).toHaveLength(0)
  })

  it('scope 非 scoped 一律归一化为 global', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        client_styles: [
          { id: 'a', path: '/a.css', scope: 'scoped' },
          { id: 'b', path: '/b.css' }, // 缺 scope → global
          { id: 'c', path: '/c.css', scope: 'weird' }, // 未知 → global
        ],
      }),
    )
    const byId = Object.fromEntries(registry.getClientStyles().map((s) => [s.id, s.scope]))
    expect(byId).toEqual({ a: 'scoped', b: 'global', c: 'global' })
  })

  it('缺 id/path 的条目被丢弃', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        client_styles: [{ path: '/no-id.css' }, { id: 'no-path' }, { id: 'ok', path: '/ok.css' }],
      }),
    )
    expect(registry.getClientStyles()).toHaveLength(1)
    expect(registry.getClientStyles()[0].id).toBe('ok')
  })

  it('client_styles 不进 pages 归一化（无幽灵页面）', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        client_styles: [{ id: 's', path: '/x.css' }],
      }),
    )
    expect(registry.getPages()).toHaveLength(0)
  })

  it('clear 清空主题与样式注册', () => {
    registry.loadFromSchema(
      contributesSchema('demo', {
        themes: [{ id: 't', name: 'T' }],
        client_styles: [{ id: 's', path: '/x.css' }],
      }),
    )
    expect(registry.getPluginThemes()).toHaveLength(1)
    expect(registry.getClientStyles()).toHaveLength(1)
    registry.clear()
    expect(registry.getPluginThemes()).toHaveLength(0)
    expect(registry.getClientStyles()).toHaveLength(0)
  })
})
