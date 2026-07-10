/**
 * CompositionEngine 测试
 *
 * 覆盖：
 * - resolve() 单体模式（直接组件引用）
 * - resolve() 组合模式（layout + children 递归）
 * - 组件从 widgetRegistry 正确解析
 * - 数据源引用解析（module://collection 格式）
 * - 嵌套组合正确处理
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { CompositionEngine } from '@/services/schema/CompositionEngine'
import type { CompositionNode, ResolvedNode } from '@/services/schema/CompositionEngine'

// ============================================================
// Mock dependencies
// ============================================================

vi.mock('@/services/schema/WidgetRegistry', () => {
  const components: Map<string, any> = new Map()
  components.set('table', () => null)
  components.set('chart', () => null)
  components.set('form', () => null)
  components.set('code_block', () => null)
  components.set('status_card', () => null)
  components.set('editor', () => null)

  return {
    widgetRegistry: {
      get: vi.fn((type: string) => components.get(type) ?? undefined),
      findFallback: vi.fn((type: string) => components.get(type) ?? undefined),
    },
  }
})

vi.mock('@/services/schema/parser', () => ({
  parseDataSourceRef: vi.fn((ref: string) => {
    const match = ref.match(/^(\w+):\/\/([^\?]+)(?:\?(.+))?$/)
    if (!match) throw new Error(`无效的数据源引用格式: ${ref}`)
    const [, moduleId, collection] = match
    return { moduleId, collection, query: {} }
  }),
  resolveDataSource: vi.fn((ref: any) => ({
    endpoint: `/api/v1/modules/${ref.moduleId}/data/${ref.collection}`,
    method: 'GET',
    params: {},
    supportsPolling: true,
  })),
}))

// ============================================================
// resolve() 单体模式
// ============================================================

describe('CompositionEngine.resolve - 单体模式', () => {
  let engine: CompositionEngine

  beforeEach(() => {
    engine = new CompositionEngine()
  })

  it('应解析单体组件引用', () => {
    const node: CompositionNode = {
      component: 'table',
      props: { columns: ['id', 'name'] },
    }
    const result = engine.resolve(node)
    expect(result.mode).toBe('single')
    expect(result.component).toBeDefined()
    expect(result.component?.type).toBe('table')
    expect(result.component?.props).toEqual({ columns: ['id', 'name'] })
  })

  it('单体组件应从 widgetRegistry 解析组件', () => {
    const node: CompositionNode = {
      component: 'chart',
      props: { chartType: 'bar' },
    }
    const result = engine.resolve(node)
    expect(result.component).toBeDefined()
    expect(result.component?.type).toBe('chart')
  })

  it('未注册组件的 component 应为 null', () => {
    const node: CompositionNode = {
      component: 'nonexistent_widget',
      props: {},
    }
    const result = engine.resolve(node)
    expect(result.component).toBeDefined()
    // component 可能是 null（降级也没找到）
    expect(result.component?.component).toBeNull()
  })

  it('空节点应返回 mode=single', () => {
    const result = engine.resolve({} as CompositionNode)
    expect(result.mode).toBe('single')
  })

  it('null 节点应返回 mode=single', () => {
    const result = engine.resolve(null as unknown as CompositionNode)
    expect(result.mode).toBe('single')
  })
})

// ============================================================
// resolve() 组合模式
// ============================================================

describe('CompositionEngine.resolve - 组合模式', () => {
  let engine: CompositionEngine

  beforeEach(() => {
    engine = new CompositionEngine()
  })

  it('应解析 layout + children 组合', () => {
    const node: CompositionNode = {
      layout: 'split-horizontal',
      children: [
        { component: 'table', props: { columns: ['id'] } },
        { component: 'chart', props: { chartType: 'bar' } },
      ],
    }
    const result = engine.resolve(node)
    expect(result.mode).toBe('composite')
    expect(result.layout).toBe('split-horizontal')
    expect(result.children).toHaveLength(2)
    expect(result.children![0].mode).toBe('single')
    expect(result.children![0].component?.type).toBe('table')
    expect(result.children![1].component?.type).toBe('chart')
  })

  it('应传递 layoutProps（ratio/defaultTab/columns）', () => {
    const node: CompositionNode = {
      layout: 'split-horizontal',
      ratio: [1, 2],
      children: [
        { component: 'table' },
        { component: 'chart' },
      ],
    }
    const result = engine.resolve(node)
    expect(result.layoutProps).toBeDefined()
    expect(result.layoutProps?.ratio).toEqual([1, 2])
  })

  it('tabs 布局应传递 defaultTab', () => {
    const node: CompositionNode = {
      layout: 'tabs',
      default_tab: 1,
      children: [
        { component: 'table', title: '表格' },
        { component: 'chart', title: '图表' },
      ],
    }
    const result = engine.resolve(node)
    expect(result.layout).toBe('tabs')
    expect(result.layoutProps?.defaultTab).toBe(1)
  })

  it('grid 布局应传递 columns', () => {
    const node: CompositionNode = {
      layout: 'grid',
      columns: 3,
      children: [
        { component: 'table' },
        { component: 'chart' },
        { component: 'form' },
      ],
    }
    const result = engine.resolve(node)
    expect(result.layout).toBe('grid')
    expect(result.layoutProps?.columns).toBe(3)
  })

  it('有 layout 但无 children 不是组合模式', () => {
    const node: CompositionNode = {
      layout: 'split-horizontal',
      component: 'table',
    }
    const result = engine.resolve(node)
    // 无 children，走单体模式
    expect(result.mode).toBe('single')
  })

  it('有 children 但无 layout 不是组合模式', () => {
    const node: CompositionNode = {
      children: [
        { component: 'table' },
      ],
      component: 'chart',
    }
    const result = engine.resolve(node)
    // 无 layout，走单体模式
    expect(result.mode).toBe('single')
  })
})

// ============================================================
// 数据源引用解析
// ============================================================

describe('CompositionEngine - 数据源引用解析', () => {
  let engine: CompositionEngine

  beforeEach(() => {
    engine = new CompositionEngine()
  })

  it('应解析 module://collection 格式数据源', () => {
    const node: CompositionNode = {
      component: 'table',
      props: {},
      data_source: 'module://items',
    }
    const result = engine.resolve(node)
    expect(result.component?.resolvedDataSource).toBeDefined()
    expect(result.component?.resolvedDataSource?.endpoint).toBe('/api/v1/modules/module/data/items')
    expect(result.component?.resolvedDataSource?.method).toBe('GET')
  })

  it('无数据源时 resolvedDataSource 应为 undefined', () => {
    const node: CompositionNode = {
      component: 'table',
      props: {},
    }
    const result = engine.resolve(node)
    expect(result.component?.resolvedDataSource).toBeUndefined()
  })

  it('无效数据源格式不应崩溃', () => {
    const node: CompositionNode = {
      component: 'table',
      props: {},
      data_source: 'invalid-format',
    }
    // 不应抛出异常，而是 warn 并继续
    const result = engine.resolve(node)
    expect(result).toBeDefined()
    expect(result.mode).toBe('single')
  })
})

// ============================================================
// 嵌套组合
// ============================================================

describe('CompositionEngine - 嵌套组合', () => {
  let engine: CompositionEngine

  beforeEach(() => {
    engine = new CompositionEngine()
  })

  it('应正确处理嵌套组合（组合中包含组合）', () => {
    const node: CompositionNode = {
      layout: 'split-horizontal',
      ratio: [1, 2],
      children: [
        {
          layout: 'tabs',
          children: [
            { component: 'table', title: '表格' },
            { component: 'chart', title: '图表' },
          ],
        },
        { component: 'form', props: { fields: [] } },
      ],
    }
    const result = engine.resolve(node)
    expect(result.mode).toBe('composite')
    expect(result.children).toHaveLength(2)

    // 第一个子节点是嵌套组合
    const nestedComposite = result.children![0]
    expect(nestedComposite.mode).toBe('composite')
    expect(nestedComposite.layout).toBe('tabs')
    expect(nestedComposite.children).toHaveLength(2)
    expect(nestedComposite.children![0].component?.type).toBe('table')
    expect(nestedComposite.children![1].component?.type).toBe('chart')

    // 第二个子节点是单体
    expect(result.children![1].mode).toBe('single')
    expect(result.children![1].component?.type).toBe('form')
  })

  it('三层嵌套应正确处理', () => {
    const node: CompositionNode = {
      layout: 'split-vertical',
      children: [
        {
          layout: 'split-horizontal',
          children: [
            { component: 'table' },
            {
              layout: 'stack',
              children: [
                { component: 'chart' },
                { component: 'form' },
              ],
            },
          ],
        },
        { component: 'code_block' },
      ],
    }
    const result = engine.resolve(node)

    // 第一层：split-vertical
    expect(result.mode).toBe('composite')
    expect(result.layout).toBe('split-vertical')
    expect(result.children).toHaveLength(2)

    // 第二层第一子：split-horizontal
    const level2 = result.children![0]
    expect(level2.mode).toBe('composite')
    expect(level2.layout).toBe('split-horizontal')
    expect(level2.children).toHaveLength(2)

    // 第三层：stack
    const level3 = level2.children![1]
    expect(level3.mode).toBe('composite')
    expect(level3.layout).toBe('stack')
    expect(level3.children).toHaveLength(2)
    expect(level3.children![0].component?.type).toBe('chart')
    expect(level3.children![1].component?.type).toBe('form')
  })
})

// ============================================================
// 辅助方法
// ============================================================

describe('CompositionEngine - 辅助方法', () => {
  let engine: CompositionEngine

  beforeEach(() => {
    engine = new CompositionEngine()
  })

  it('isComposite 应正确判断', () => {
    const compositeNode: CompositionNode = {
      layout: 'grid',
      children: [{ component: 'table' }],
    }
    const singleNode: CompositionNode = {
      component: 'table',
    }
    expect(engine.isComposite(compositeNode)).toBe(true)
    expect(engine.isComposite(singleNode)).toBe(false)
  })

  it('isSingle 应正确判断', () => {
    const compositeNode: CompositionNode = {
      layout: 'grid',
      children: [{ component: 'table' }],
    }
    const singleNode: CompositionNode = {
      component: 'table',
    }
    expect(engine.isSingle(compositeNode)).toBe(false)
    expect(engine.isSingle(singleNode)).toBe(true)
  })

  it('getSupportedLayouts 返回所有布局类型', () => {
    const layouts = engine.getSupportedLayouts()
    expect(layouts).toContain('split-horizontal')
    expect(layouts).toContain('split-vertical')
    expect(layouts).toContain('tabs')
    expect(layouts).toContain('grid')
    expect(layouts).toContain('stack')
    expect(layouts).toHaveLength(5)
  })

  it('resolveAll 批量解析', () => {
    const nodes: CompositionNode[] = [
      { component: 'table' },
      { component: 'chart' },
      {
        layout: 'split-horizontal',
        children: [
          { component: 'table' },
          { component: 'form' },
        ],
      },
    ]
    const results = engine.resolveAll(nodes)
    expect(results).toHaveLength(3)
    expect(results[0].mode).toBe('single')
    expect(results[1].mode).toBe('single')
    expect(results[2].mode).toBe('composite')
  })

  it('tabMeta 应正确传递', () => {
    const node: CompositionNode = {
      component: 'table',
      title: '数据表格',
      icon: '📊',
    }
    const result = engine.resolve(node)
    expect(result.tabMeta).toBeDefined()
    expect(result.tabMeta?.title).toBe('数据表格')
    expect(result.tabMeta?.icon).toBe('📊')
  })
})
