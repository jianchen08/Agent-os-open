// @feature FP-T12 前端组件补测
/**
 * hindsight_memory 声明页承接测试（/memory、/knowledge-base 路由退役配套）
 *
 * 核验声明↔注册闭环：hindsight_memory contributes.pages 声明的
 * memory_panel / knowledge_base_panel 注册名在预置注册表可解析——
 * /p/memory、/p/knowledge_base 经 PluginPageRenderer → renderPageContent
 * widget 分支取到组件（声明在插件、注册名在前端，断链则全页 404 占位）。
 */

import { describe, expect, it } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

/** 以 hindsight_memory plugin.json 真实声明形态装载（等价 GrowthLoop → loadFromSchema） */
contributionRegistry.loadFromSchema({
  plugin_contributes: [
    {
      plugin_id: 'hindsight_memory',
      plugin_name: 'Hindsight Memory',
      contributes: {
        pages: [
          { id: 'memory', title: '记忆管理', icon: '🧠', space: 'workspace', slot: 'tab', widget: 'memory_panel' },
          { id: 'knowledge_base', title: '知识库', icon: '📚', space: 'workspace', slot: 'tab', widget: 'knowledge_base_panel' },
        ],
      },
    },
  ],
  plugin_configs: [],
})

describe('hindsight_memory 声明页承接', () => {
  it('声明页 id 可解析且 widget 注册名在预置注册表命中', () => {
    initializeWidgets()
    for (const pageId of ['memory', 'knowledge_base'] as const) {
      const page = contributionRegistry.getPage(pageId)
      expect(page, `page ${pageId} 应可解析`).toBeDefined()
      expect(widgetRegistry.get(page!.widget!)).toBeDefined()
    }
  })

  it('声明不带 path/slot=tab → 不进侧栏入口也不劫持路由 path', () => {
    const pages = contributionRegistry.getPagesBySpace('workspace')
    const sidebar = pages.filter((p) => p.slot === 'activity-bar')
    expect(sidebar.map((p) => p.id)).not.toContain('memory')
    expect(sidebar.map((p) => p.id)).not.toContain('knowledge_base')
    expect(pages.find((p) => p.id === 'memory')?.path).toBeUndefined()
  })
})
