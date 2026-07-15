/**
 * SchemaRouter 测试
 *
 * 覆盖 AC-11-7: Schema 路由表——widget_type → 渲染空间映射可动态扩展
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { SchemaRouter } from '@/services/schema/SchemaRouter'

describe('SchemaRouter — 默认路由表', () => {
  let router: SchemaRouter

  beforeEach(() => {
    router = new SchemaRouter()
  })

  it('默认路由表包含常用 widget_type 映射', () => {
    expect(router.resolve('review_document')).toBe('workspace')
    expect(router.resolve('image_viewer')).toBe('chat')
    expect(router.resolve('floating_assistant')).toBe('floating')
    expect(router.resolve('custom_tool_panel')).toBe('dock')
  })

  it('未知 widget_type 返回默认空间 chat', () => {
    expect(router.resolve('unknown_widget')).toBe('chat')
  })
})

describe('SchemaRouter — 自定义路由注册', () => {
  let router: SchemaRouter

  beforeEach(() => {
    router = new SchemaRouter()
  })

  it('可注册自定义 widget_type → render_space 映射', () => {
    router.register('digital_human', 'scene')
    expect(router.resolve('digital_human')).toBe('scene')
  })

  it('注册后不影响默认路由', () => {
    router.register('custom_w', 'dock')
    expect(router.resolve('review_document')).toBe('workspace')
    expect(router.resolve('custom_w')).toBe('dock')
  })

  it('批量注册路由', () => {
    router.registerAll({
      widget_a: 'workspace',
      widget_b: 'dock',
      widget_c: 'scene',
    })
    expect(router.resolve('widget_a')).toBe('workspace')
    expect(router.resolve('widget_b')).toBe('dock')
    expect(router.resolve('widget_c')).toBe('scene')
  })

  it('覆盖已有路由', () => {
    router.register('review_document', 'dock')
    expect(router.resolve('review_document')).toBe('dock')
  })
})

describe('SchemaRouter — 从 ui_contributions 批量注册', () => {
  let router: SchemaRouter

  beforeEach(() => {
    router = new SchemaRouter()
  })

  it('从 ui_contributions 列表注册路由', () => {
    router.registerFromContributions([
      { type: 'widget', widgetType: 'custom_a', renderSpace: 'workspace' },
      { type: 'panel', widgetType: 'custom_b', renderSpace: 'dock' },
    ])
    expect(router.resolve('custom_a')).toBe('workspace')
    expect(router.resolve('custom_b')).toBe('dock')
  })

  it('ui_contributions 的 renderSpace 优先于默认路由', () => {
    // 默认 review_document → workspace
    expect(router.resolve('review_document')).toBe('workspace')

    router.registerFromContributions([
      { type: 'widget', widgetType: 'review_document', renderSpace: 'dock' },
    ])
    expect(router.resolve('review_document')).toBe('dock')
  })
})

describe('SchemaRouter — 路由查询', () => {
  let router: SchemaRouter

  beforeEach(() => {
    router = new SchemaRouter()
    router.registerAll({
      w1: 'workspace',
      w2: 'dock',
      w3: 'floating',
    })
  })

  it('listRoutes 返回所有自定义路由', () => {
    const routes = router.listRoutes()
    expect(routes.size).toBeGreaterThan(3)
    expect(routes.get('w1')).toBe('workspace')
    expect(routes.get('w2')).toBe('dock')
  })

  it('getRoutesForSpace 返回指定空间的所有 widget_type', () => {
    const dockWidgets = router.getRoutesForSpace('dock')
    expect(dockWidgets).toContain('w2')
  })
})
