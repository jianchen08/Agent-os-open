// @feature FP-T12 前端组件补测
/**
 * hindsight_memory 声明页承接测试（记忆单页聚合，2026-09 用户裁定）
 *
 * 核验声明↔注册闭环，声明直读真实 plugin.json（与生产 /api/v1/schema 同源）：
 * - workspace 页声明只含 memory——knowledge_base 已并入 memory 页「文档库」
 *   分区，页声明摘除（导航页知识库卡片随之消失，无前端特判）
 * - memory_panel 注册名在预置注册表可解析（/p/memory 经 PluginPageRenderer
 *   → renderPageContent widget 分支取到组件，断链则全页 404 占位）
 * - knowledge_base_panel 死注册不残留
 */

import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

/** 直读真实 plugin.json（生产 schema 源头） */
const PLUGIN_JSON_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/shared/system/hindsight_memory/plugin.json',
)
const pluginJson = JSON.parse(readFileSync(PLUGIN_JSON_PATH, 'utf-8')) as {
  contributes: { pages: Array<{ id: string; space: string; slot: string; widget: string }> }
}

/** 以 hindsight_memory 真实声明形态装载（等价 GrowthLoop → loadFromSchema） */
contributionRegistry.loadFromSchema({
  plugin_contributes: [
    {
      plugin_id: 'hindsight_memory',
      plugin_name: 'Hindsight Memory',
      contributes: pluginJson.contributes,
    },
  ],
  plugin_configs: [],
})

describe('hindsight_memory 声明页承接', () => {
  it('workspace 页声明只含 memory：knowledge_base 并入后摘除', () => {
    const pageIds = contributionRegistry
      .getPagesBySpace('workspace')
      .filter((p) => p.id === 'memory' || p.id === 'knowledge_base')
      .map((p) => p.id)
    expect(pageIds).toEqual(['memory'])
  })

  it('memory 声明经 memory_panel 注册名在预置注册表命中', () => {
    initializeWidgets()
    const page = contributionRegistry.getPage('memory')
    expect(page, 'page memory 应可解析').toBeDefined()
    expect(page!.widget).toBe('memory_panel')
    expect(widgetRegistry.get('memory_panel')).toBeDefined()
  })

  it('knowledge_base_panel 死注册不残留', () => {
    initializeWidgets()
    expect(widgetRegistry.get('knowledge_base_panel')).toBeUndefined()
  })

  it('声明不带 path/slot=tab → 不进侧栏入口也不劫持路由 path', () => {
    const pages = contributionRegistry.getPagesBySpace('workspace')
    const sidebar = pages.filter((p) => p.slot === 'activity-bar')
    expect(sidebar.map((p) => p.id)).not.toContain('memory')
    expect(pages.find((p) => p.id === 'memory')?.path).toBeUndefined()
  })
})
