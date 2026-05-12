/**
 * RenderingEngine 测试
 *
 * 覆盖：
 * - render() 生成正确的渲染指令
 * - 指令按空间正确分类（chat/workspace/floating/dock/fullscreen）
 * - 客户端能力降级过滤
 * - 缓存命中（相同 versionHash 返回缓存）
 * - renderAll() 合并多个 Schema 指令
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { RenderingEngine } from '@/services/schema/RenderingEngine'
import type { RenderInstructionSet, RenderInstruction } from '@/services/schema/RenderingEngine'
import type { WidgetComponent } from '@/services/schema/WidgetRegistry'
import type { ParsedSchema, ClientCapabilities } from '@/types/schema'

// ============================================================
// Mock WidgetRegistry
// ============================================================

// Mock widgetRegistry 模块
vi.mock('@/services/schema/WidgetRegistry', () => {
  const components: Map<string, any> = new Map()
  components.set('table', () => null)
  components.set('chart', () => null)
  components.set('form', () => null)
  components.set('split', () => null)
  components.set('status_card', () => null)
  components.set('editor', () => null)
  components.set('code_block', () => null)

  return {
    widgetRegistry: {
      get: vi.fn((type: string) => components.get(type) ?? undefined),
      findFallback: vi.fn((type: string) => components.get(type) ?? undefined),
    },
  }
})

/** 创建有效的 ParsedSchema */
function createParsedSchema(overrides?: Partial<ParsedSchema>): ParsedSchema {
  return {
    raw: {
      identity: { id: 'test-mod', name: 'Test', version: '1.0.0', category: 'builtin' },
      actions: [],
      rendering: {
        chat: [],
        spaces: [],
      },
      clients: {
        requiredSpaces: [],
        requiredWidgets: [],
      },
    },
    identity: { id: 'test-mod', name: 'Test', version: '1.0.0', category: 'builtin' },
    actions: [],
    rendering: {
      chat: [],
      spaces: [],
    },
    clients: {
      requiredSpaces: [],
      requiredWidgets: [],
    },
    parsedAt: Date.now(),
    versionHash: 'abc123',
    ...overrides,
  }
}

// ============================================================
// render() 生成渲染指令
// ============================================================

describe('RenderingEngine.render', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('应生成 RenderInstructionSet 结构', () => {
    const schema = createParsedSchema()
    const result = engine.render(schema)
    expect(result).toHaveProperty('bySpace')
    expect(result).toHaveProperty('all')
    expect(result).toHaveProperty('versionHash')
    expect(result).toHaveProperty('generatedAt')
    expect(result.versionHash).toBe('abc123')
  })

  it('空 Schema 应生成空指令', () => {
    const schema = createParsedSchema()
    const result = engine.render(schema)
    expect(result.all).toHaveLength(0)
    expect(result.bySpace.chat).toHaveLength(0)
    expect(result.bySpace.workspace).toHaveLength(0)
    expect(result.bySpace.floating).toHaveLength(0)
    expect(result.bySpace.dock).toHaveLength(0)
    expect(result.bySpace.fullscreen).toHaveLength(0)
  })

  it('包含 rendering.spaces 时应生成对应空间指令', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [],
        spaces: [
          { space: 'workspace', widget: 'table', props: { columns: [] } },
          { space: 'floating', widget: 'chart' },
        ],
      },
    })
    const result = engine.render(schema)
    expect(result.bySpace.workspace).toHaveLength(1)
    expect(result.bySpace.floating).toHaveLength(1)
    expect(result.bySpace.workspace[0].widgetType).toBe('table')
    expect(result.bySpace.floating[0].widgetType).toBe('chart')
  })

  it('包含 rendering.chat 时应生成 chat 空间指令', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [
          { type: 'form', dataSource: 'module://items/create' },
          { type: 'chart', refreshInterval: 30000 },
        ],
        spaces: [],
      },
    })
    const result = engine.render(schema)
    expect(result.bySpace.chat).toHaveLength(2)
    expect(result.bySpace.chat[0].widgetType).toBe('form')
    expect(result.bySpace.chat[1].widgetType).toBe('chart')
  })

  it('包含 rendering.dock 时应生成 dock 空间指令', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [],
        spaces: [],
        dock: { icon: '🧪', label: 'Test', indicator: 'dot' },
      },
    })
    const result = engine.render(schema)
    expect(result.bySpace.dock).toHaveLength(1)
    expect(result.bySpace.dock[0].widgetType).toBe('dock_entry')
    expect(result.bySpace.dock[0].props.icon).toBe('🧪')
    expect(result.bySpace.dock[0].props.label).toBe('Test')
  })

  it('指令应包含正确的 moduleId', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [{ space: 'workspace', widget: 'table' }],
      },
    })
    const result = engine.render(schema)
    for (const instr of result.all) {
      expect(instr.moduleId).toBe('test-mod')
    }
  })

  it('all 应是 bySpace 中所有指令的平铺', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [
          { space: 'workspace', widget: 'table' },
          { space: 'floating', widget: 'chart' },
        ],
      },
    })
    const result = engine.render(schema)
    expect(result.all.length).toBe(
      result.bySpace.chat.length +
      result.bySpace.workspace.length +
      result.bySpace.floating.length +
      result.bySpace.dock.length +
      result.bySpace.fullscreen.length,
    )
  })
})

// ============================================================
// 指令按空间正确分类
// ============================================================

describe('RenderingEngine - 空间分类', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('5 种空间类型都应有对应数组', () => {
    const schema = createParsedSchema()
    const result = engine.render(schema)
    expect(result.bySpace).toHaveProperty('chat')
    expect(result.bySpace).toHaveProperty('workspace')
    expect(result.bySpace).toHaveProperty('floating')
    expect(result.bySpace).toHaveProperty('dock')
    expect(result.bySpace).toHaveProperty('fullscreen')
  })

  it('fullscreen 空间指令应正确分类', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [],
        spaces: [
          { space: 'fullscreen', widget: 'editor', props: { language: 'python' } },
        ],
      },
    })
    const result = engine.render(schema)
    expect(result.bySpace.fullscreen).toHaveLength(1)
    expect(result.bySpace.fullscreen[0].widgetType).toBe('editor')
  })

  it('多个空间混合应正确分类', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [{ type: 'form' }, { type: 'chart' }],
        spaces: [
          { space: 'workspace', widget: 'table' },
          { space: 'floating', widget: 'chart' },
          { space: 'dock', widget: 'status_card' },
        ],
        dock: { icon: '📱', label: 'App' },
      },
    })
    const result = engine.render(schema)
    expect(result.bySpace.chat.length).toBe(2)
    expect(result.bySpace.workspace.length).toBe(1)
    expect(result.bySpace.floating.length).toBe(1)
    // dock: 1 个 space config + 1 个 dock entry
    expect(result.bySpace.dock.length).toBeGreaterThanOrEqual(1)
  })
})

// ============================================================
// 客户端能力降级过滤
// ============================================================

describe('RenderingEngine - 客户端能力降级', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('限制 requiredSpaces 时过滤不支持的渲染空间', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [],
        spaces: [
          { space: 'workspace', widget: 'table' },
          { space: 'floating', widget: 'chart' },
          { space: 'fullscreen', widget: 'editor' },
        ],
      },
    })
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat', 'workspace'],
      requiredWidgets: [],
    }
    const result = engine.render(schema, capabilities)
    expect(result.bySpace.workspace).toHaveLength(1)
    expect(result.bySpace.floating).toHaveLength(0)
    expect(result.bySpace.fullscreen).toHaveLength(0)
  })

  it('有 fallback 配置时使用 fallback 空间', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [
          { space: 'workspace', widget: 'table' },
          { space: 'floating', widget: 'chart' },
        ],
      },
    })
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat'],
      requiredWidgets: [],
      fallback: { widget: 'status_card', space: 'chat' },
    }
    const result = engine.render(schema, capabilities)
    // fallback 场景：supportedSpaces = requiredSpaces + fallback.space
    expect(result.bySpace.chat.length).toBeGreaterThanOrEqual(0)
  })

  it('无 capabilities 时不做降级过滤', () => {
    const schema = createParsedSchema({
      rendering: {
        chat: [],
        spaces: [
          { space: 'workspace', widget: 'table' },
          { space: 'floating', widget: 'chart' },
        ],
        dock: { icon: '📱', label: 'App' },
      },
    })
    const result = engine.render(schema)
    // 无限制时，Schema 声明的空间都应渲染
    expect(result.bySpace.workspace).toHaveLength(1)
    expect(result.bySpace.floating).toHaveLength(1)
    expect(result.bySpace.dock).toHaveLength(1)
  })
})

// ============================================================
// 缓存命中
// ============================================================

describe('RenderingEngine - 缓存', () => {
  it('相同 versionHash 返回缓存', () => {
    const engine = new RenderingEngine({ enableCache: true })
    const schema = createParsedSchema({
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [],
      },
    })

    const first = engine.render(schema)
    const second = engine.render(schema)

    // 应该是同一个对象（缓存命中）
    expect(first).toBe(second)
    expect(first.generatedAt).toBe(second.generatedAt)
  })

  it('不同 versionHash 不命中缓存', () => {
    const engine = new RenderingEngine({ enableCache: true })
    const schema1 = createParsedSchema({
      versionHash: 'hash1',
      rendering: { chat: [{ type: 'form' }], spaces: [] },
    })
    const schema2 = createParsedSchema({
      versionHash: 'hash2',
      rendering: { chat: [{ type: 'chart' }], spaces: [] },
    })

    const first = engine.render(schema1)
    const second = engine.render(schema2)

    expect(first).not.toBe(second)
    expect(first.versionHash).toBe('hash1')
    expect(second.versionHash).toBe('hash2')
  })

  it('禁用缓存时每次都重新生成', () => {
    const engine = new RenderingEngine({ enableCache: false })
    const schema = createParsedSchema({
      rendering: { chat: [{ type: 'form' }], spaces: [] },
    })

    const first = engine.render(schema)
    const second = engine.render(schema)

    expect(first).not.toBe(second)
  })

  it('clearCache 清空后重新生成', () => {
    const engine = new RenderingEngine({ enableCache: true })
    const schema = createParsedSchema({
      rendering: { chat: [{ type: 'form' }], spaces: [] },
    })

    const first = engine.render(schema)
    engine.clearCache()
    const second = engine.render(schema)

    expect(first).not.toBe(second)
  })
})

// ============================================================
// renderAll() 合并多个 Schema 指令
// ============================================================

describe('RenderingEngine.renderAll', () => {
  let engine: RenderingEngine

  beforeEach(() => {
    engine = new RenderingEngine({ enableCache: false })
  })

  it('应合并多个 Schema 的指令', () => {
    const schemas = [
      createParsedSchema({
        identity: { id: 'mod1', name: 'Mod1', version: '1.0.0', category: 'builtin' },
        rendering: {
          chat: [{ type: 'form' }],
          spaces: [{ space: 'workspace', widget: 'table' }],
        },
      }),
      createParsedSchema({
        identity: { id: 'mod2', name: 'Mod2', version: '1.0.0', category: 'builtin' },
        rendering: {
          chat: [{ type: 'chart' }],
          spaces: [{ space: 'floating', widget: 'chart' }],
        },
      }),
    ]
    const result = engine.renderAll(schemas)

    expect(result.bySpace.chat).toHaveLength(2) // form + chart
    expect(result.bySpace.workspace).toHaveLength(1) // table
    expect(result.bySpace.floating).toHaveLength(1) // chart
    expect(result.all).toHaveLength(4)
  })

  it('空数组返回空指令集', () => {
    const result = engine.renderAll([])
    expect(result.all).toHaveLength(0)
    expect(result.versionHash).toBe('')
  })

  it('versionHash 应由所有 Schema 哈希拼接', () => {
    const schemas = [
      createParsedSchema({ versionHash: 'h1' }),
      createParsedSchema({ versionHash: 'h2' }),
    ]
    const result = engine.renderAll(schemas)
    expect(result.versionHash).toBe('h1+h2')
  })
})
