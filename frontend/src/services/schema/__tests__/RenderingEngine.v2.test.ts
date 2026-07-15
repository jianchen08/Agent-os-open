/**
 * RenderingEngine 0.2 扩展测试
 *
 * 覆盖 AC-11-6: 5 渲染空间可接收插件贡献的 Widget
 * 覆盖 AC-11-7: 渲染空间扩展机制可用——第三方插件可通过 manifest 声明自定义 Widget
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { RenderingEngine } from '@/services/schema/RenderingEngine'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { SchemaRouter } from '@/services/schema/SchemaRouter'
import type { ParsedSchema, UIContribution } from '@/types/schema'

/** 创建包含 ui_contributions 的 ParsedSchema */
function createParsedSchemaWithContributions(
  contributions: UIContribution[],
): ParsedSchema {
  return {
    raw: {} as any,
    identity: {
      id: 'test-plugin',
      name: 'Test Plugin',
      version: '2.0.0',
      category: 'extension',
    },
    actions: [],
    rendering: {
      chat: [],
      spaces: [],
    },
    clients: {
      requiredSpaces: ['chat', 'workspace', 'floating', 'dock', 'scene'],
      requiredWidgets: [],
    },
    ui_contributions: contributions,
    parsedAt: Date.now(),
    versionHash: 'test-hash-v2',
  }
}

describe('RenderingEngine 0.2 — AC-11-6: ui_contributions → 渲染空间路由', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('workspace 类型贡献项路由到 workspace 渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'review_document',
        renderSpace: 'workspace',
        label: '文档审阅',
      },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.workspace.length).toBeGreaterThan(0)
    const instruction = result.bySpace.workspace.find(
      (i) => i.widgetType === 'review_document',
    )
    expect(instruction).toBeDefined()
    expect(instruction?.space).toBe('workspace')
  })

  it('dock 类型贡献项路由到 dock 渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'panel',
        widgetType: 'review_toolbar',
        renderSpace: 'dock',
        label: '审阅工具栏',
      },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.dock.length).toBeGreaterThan(0)
    const instruction = result.bySpace.dock.find(
      (i) => i.widgetType === 'review_toolbar',
    )
    expect(instruction).toBeDefined()
    expect(instruction?.space).toBe('dock')
  })

  it('floating 类型贡献项路由到 floating 渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'floating_assistant',
        renderSpace: 'floating',
        label: '悬浮助手',
      },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.floating.length).toBeGreaterThan(0)
    const instruction = result.bySpace.floating.find(
      (i) => i.widgetType === 'floating_assistant',
    )
    expect(instruction).toBeDefined()
  })

  it('scene 类型贡献项路由到 scene 渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'digital_human',
        renderSpace: 'scene',
        label: '数字人',
      },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.scene.length).toBeGreaterThan(0)
  })

  it('chat 类型贡献项路由到 chat 渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'image_viewer',
        renderSpace: 'chat',
        label: '图片查看器',
      },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.chat.length).toBeGreaterThan(0)
  })

  it('多个贡献项路由到各自渲染空间', () => {
    const schema = createParsedSchemaWithContributions([
      { type: 'widget', widgetType: 'w1', renderSpace: 'workspace' },
      { type: 'panel', widgetType: 'w2', renderSpace: 'dock' },
      { type: 'widget', widgetType: 'w3', renderSpace: 'floating' },
      { type: 'widget', widgetType: 'w4', renderSpace: 'scene' },
    ])
    const result = engine.render(schema)

    expect(result.bySpace.workspace.length).toBeGreaterThan(0)
    expect(result.bySpace.dock.length).toBeGreaterThan(0)
    expect(result.bySpace.floating.length).toBeGreaterThan(0)
    expect(result.bySpace.scene.length).toBeGreaterThan(0)
  })
})

describe('RenderingEngine 0.2 — AC-11-7: 渲染指令包含贡献项元数据', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('渲染指令包含 widgetType 和 datasourceUri', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'dynamic_table',
        renderSpace: 'workspace',
        datasourceUri: 'datasource://data/table',
        schema: { columns: ['name', 'value'] },
      },
    ])
    const result = engine.render(schema)

    const instruction = result.bySpace.workspace.find(
      (i) => i.widgetType === 'dynamic_table',
    )
    expect(instruction).toBeDefined()
    expect(instruction?.dataSource).toBe('datasource://data/table')
  })

  it('渲染指令 moduleId 指向来源插件', () => {
    const schema = createParsedSchemaWithContributions([
      {
        type: 'widget',
        widgetType: 'custom_widget',
        renderSpace: 'workspace',
      },
    ])
    const result = engine.render(schema)

    const instruction = result.bySpace.workspace[0]
    expect(instruction.moduleId).toBe('test-plugin')
  })
})

describe('RenderingEngine 0.2 — 5 渲染空间完整覆盖', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('all 指令包含所有 5 个渲染空间的贡献', () => {
    const schema = createParsedSchemaWithContributions([
      { type: 'widget', widgetType: 'chat_w', renderSpace: 'chat' },
      { type: 'widget', widgetType: 'ws_w', renderSpace: 'workspace' },
      { type: 'widget', widgetType: 'float_w', renderSpace: 'floating' },
      { type: 'panel', widgetType: 'dock_w', renderSpace: 'dock' },
      { type: 'widget', widgetType: 'scene_w', renderSpace: 'scene' },
    ])
    const result = engine.render(schema)

    const spacesInAll = new Set(result.all.map((i) => i.space))
    expect(spacesInAll.has('chat')).toBe(true)
    expect(spacesInAll.has('workspace')).toBe(true)
    expect(spacesInAll.has('floating')).toBe(true)
    expect(spacesInAll.has('dock')).toBe(true)
    expect(spacesInAll.has('scene')).toBe(true)
  })
})
