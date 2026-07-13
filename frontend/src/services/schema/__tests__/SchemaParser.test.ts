/**
 * SchemaParser 测试
 *
 * 覆盖：
 * - parse() 正确解析有效 Schema
 * - parse() 对无效 Schema 抛出 SchemaParseError
 * - 版本哈希计算一致性
 * - 增量更新检测（相同 Schema 返回 changed=false）
 * - validate() 检测缺失字段
 * - filterByCapabilities() 按客户端能力过滤
 */

import { describe, it, expect, beforeEach } from 'vitest'
import {
  SchemaParser,
  SchemaParseError,
} from '@/services/schema/SchemaParser'
import type {
  ModuleUISchema,
  ClientCapabilities,
} from '@/types/schema'

/** 创建有效的测试 Schema */
function createValidSchema(overrides?: Partial<ModuleUISchema>): ModuleUISchema {
  return {
    identity: {
      id: 'test-module',
      name: 'Test Module',
      version: '1.0.0',
      category: 'builtin',
      description: 'A test module',
      icon: '🧪',
      author: 'tester',
      tags: ['test'],
    },
    actions: [
      { id: 'run', name: '运行', type: 'command' },
      { id: 'query', name: '查询', type: 'query' },
    ],
    rendering: {
      chat: [
        { type: 'form', dataSource: 'module://items/create' },
        { type: 'chart', refreshInterval: 30000 },
      ],
      spaces: [
        { space: 'workspace', widget: 'table', props: { columns: [] } },
        { space: 'floating', widget: 'chart' },
      ],
    },
    clients: {
      requiredSpaces: ['chat', 'workspace'],
      requiredWidgets: ['form', 'chart'],
    },
    ...overrides,
  }
}

// ============================================================
// parse() 正确解析
// ============================================================

describe('SchemaParser.parse', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('应正确解析有效的 ModuleUISchema', () => {
    const schema = createValidSchema()
    const { parsed, changed } = parser.parse(schema)

    expect(changed).toBe(true)
    expect(parsed.identity.id).toBe('test-module')
    expect(parsed.identity.name).toBe('Test Module')
    expect(parsed.actions).toHaveLength(2)
    expect(parsed.rendering.chat).toHaveLength(2)
    expect(parsed.rendering.spaces).toHaveLength(2)
    expect(parsed.clients.requiredSpaces).toEqual(['chat', 'workspace'])
    expect(parsed.versionHash).toBeDefined()
    expect(parsed.parsedAt).toBeGreaterThan(0)
  })

  it('应深拷贝原始 Schema 数据', () => {
    const schema = createValidSchema()
    const { parsed } = parser.parse(schema)

    // 修改 parsed 不应影响原始数据
    parsed.identity.id = 'modified'
    expect(schema.identity.id).toBe('test-module')
  })

  it('解析结果 raw 字段应指向原始 Schema', () => {
    const schema = createValidSchema()
    const { parsed } = parser.parse(schema)
    expect(parsed.raw).toBe(schema)
  })
})

// ============================================================
// parse() 无效 Schema 抛出 SchemaParseError
// ============================================================

describe('SchemaParser.parse - 无效 Schema', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('缺少 identity 应抛出 SchemaParseError', () => {
    const invalid = { ...createValidSchema(), identity: undefined } as unknown as ModuleUISchema
    expect(() => parser.parse(invalid)).toThrow(SchemaParseError)
  })

  it('identity.id 为空应抛出 SchemaParseError', () => {
    const invalid = createValidSchema({
      identity: { ...createValidSchema().identity, id: '' },
    })
    expect(() => parser.parse(invalid)).toThrow(SchemaParseError)
  })

  it('identity.name 为空应抛出 SchemaParseError', () => {
    const invalid = createValidSchema({
      identity: { ...createValidSchema().identity, name: '' },
    })
    expect(() => parser.parse(invalid)).toThrow(SchemaParseError)
  })

  it('identity.version 为空应抛出 SchemaParseError', () => {
    const invalid = createValidSchema({
      identity: { ...createValidSchema().identity, version: '' as unknown as undefined },
    })
    expect(() => parser.parse(invalid)).toThrow(SchemaParseError)
  })

  it('null 输入应抛出 SchemaParseError', () => {
    expect(() => parser.parse(null as unknown as ModuleUISchema)).toThrow(SchemaParseError)
  })

  it('undefined 输入应抛出 SchemaParseError', () => {
    expect(() => parser.parse(undefined as unknown as ModuleUISchema)).toThrow(SchemaParseError)
  })

  it('actions 不是数组应抛出 SchemaParseError', () => {
    const invalid = { ...createValidSchema(), actions: 'not-array' } as unknown as ModuleUISchema
    expect(() => parser.parse(invalid)).toThrow(SchemaParseError)
  })
})

// ============================================================
// 版本哈希计算一致性
// ============================================================

describe('SchemaParser - 版本哈希', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('相同 Schema 多次解析产生相同 versionHash', () => {
    const schema = createValidSchema()
    const { parsed: first } = parser.parse(schema)
    parser.clearCache()
    const { parsed: second } = parser.parse(schema)
    expect(first.versionHash).toBe(second.versionHash)
  })

  it('不同 Schema 产生不同 versionHash', () => {
    const schema1 = createValidSchema()
    const schema2 = createValidSchema({
      identity: { ...createValidSchema().identity, id: 'different-module' },
    })
    const { parsed: p1 } = parser.parse(schema1)
    parser.clearCache()
    const { parsed: p2 } = parser.parse(schema2)
    expect(p1.versionHash).not.toBe(p2.versionHash)
  })
})

// ============================================================
// 增量更新检测
// ============================================================

describe('SchemaParser - 增量更新', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('相同 Schema 第二次解析返回 changed=false', () => {
    const schema = createValidSchema()
    const first = parser.parse(schema)
    expect(first.changed).toBe(true)

    const second = parser.parse(schema)
    expect(second.changed).toBe(false)
    expect(second.parsed.versionHash).toBe(first.parsed.versionHash)
  })

  it('Schema 变更后返回 changed=true', () => {
    const schema = createValidSchema()
    parser.parse(schema)

    const updated = createValidSchema({
      identity: { ...createValidSchema().identity, version: '2.0.0' },
    })
    const result = parser.parse(updated)
    expect(result.changed).toBe(true)
  })

  it('parseUpdates 只返回有变化的结果', () => {
    const schema1 = createValidSchema()
    const schema2 = createValidSchema({
      identity: { ...createValidSchema().identity, id: 'mod2', name: 'Mod2' },
    })
    parser.parseAll([schema1, schema2])

    // 再次解析相同数据
    const updates = parser.parseUpdates([schema1, schema2])
    expect(updates).toHaveLength(0)

    // 修改 schema2
    const schema2Updated = createValidSchema({
      identity: { ...createValidSchema().identity, id: 'mod2', name: 'Mod2 Updated' },
    })
    const updates2 = parser.parseUpdates([schema1, schema2Updated])
    expect(updates2).toHaveLength(1)
    expect(updates2[0].parsed.identity.name).toBe('Mod2 Updated')
  })
})

// ============================================================
// validate() 验证缺失字段
// ============================================================

describe('SchemaParser.validate', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('有效 Schema 验证通过', () => {
    const schema = createValidSchema()
    const result = parser.validate(schema)
    expect(result.valid).toBe(true)
    expect(result.errors).toHaveLength(0)
  })

  it('缺少 identity 验证失败', () => {
    const result = parser.validate({ actions: [] })
    expect(result.valid).toBe(false)
    expect(result.errors).toContain('缺少 identity 字段')
  })

  it('identity.id 为空验证失败', () => {
    const result = parser.validate({
      identity: { name: 'Test', version: '1.0.0' },
      actions: [],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [] },
    })
    expect(result.valid).toBe(false)
    expect(result.errors.some(e => e.includes('identity.id'))).toBe(true)
  })

  it('identity.name 为空验证失败', () => {
    const result = parser.validate({
      identity: { id: 'test', version: '1.0.0' },
      actions: [],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [] },
    })
    expect(result.valid).toBe(false)
    expect(result.errors.some(e => e.includes('identity.name'))).toBe(true)
  })

  it('actions 不是数组验证失败', () => {
    const result = parser.validate({
      identity: { id: 'test', name: 'Test', version: '1.0.0' },
      actions: 'not-array',
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [] },
    })
    expect(result.valid).toBe(false)
    expect(result.errors).toContain('actions 必须是数组')
  })

  it('null 输入验证失败', () => {
    const result = parser.validate(null)
    expect(result.valid).toBe(false)
    expect(result.errors.length).toBeGreaterThan(0)
  })

  it('action 缺少 id 验证失败', () => {
    const result = parser.validate({
      identity: { id: 'test', name: 'Test', version: '1.0.0' },
      actions: [{ name: 'No ID' }],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [] },
    })
    expect(result.valid).toBe(false)
    expect(result.errors.some(e => e.includes('actions[0].id'))).toBe(true)
  })

  it('非标准 category 应产生警告', () => {
    const result = parser.validate({
      identity: { id: 'test', name: 'Test', version: '1.0.0', category: 'unknown' },
      actions: [],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [] },
    })
    // 注意：这可能是 warning 而非 error
    expect(result.warnings.some(w => w.includes('category'))).toBe(true)
  })
})

// ============================================================
// filterByCapabilities()
// ============================================================

describe('SchemaParser.filterByCapabilities', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('按 requiredSpaces 过滤渲染空间', () => {
    const schema = createValidSchema()
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat'],
      requiredWidgets: [],
    }
    const filtered = parser.filterByCapabilities(schema, capabilities)
    expect(filtered.rendering.spaces).toHaveLength(0) // workspace 不在 ['chat'] 中
  })

  it('按 requiredWidgets 过滤 chat 交互组件', () => {
    const schema = createValidSchema()
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat', 'workspace'],
      requiredWidgets: ['form'],
    }
    const filtered = parser.filterByCapabilities(schema, capabilities)
    expect(filtered.rendering.chat).toHaveLength(1)
    expect(filtered.rendering.chat[0].type).toBe('form')
  })

  it('requiredWidgets 为空时不过滤 chat 组件', () => {
    const schema = createValidSchema()
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat', 'workspace'],
      requiredWidgets: [],
    }
    const filtered = parser.filterByCapabilities(schema, capabilities)
    expect(filtered.rendering.chat).toHaveLength(2)
  })

  it('过滤后不影响原始 Schema', () => {
    const schema = createValidSchema()
    const capabilities: ClientCapabilities = {
      requiredSpaces: ['chat'],
      requiredWidgets: [],
    }
    parser.filterByCapabilities(schema, capabilities)
    // 原始 Schema 不应被修改
    expect(schema.rendering.spaces).toHaveLength(2)
  })
})

// ============================================================
// 缓存管理
// ============================================================

describe('SchemaParser - 缓存管理', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('getCached 返回缓存的解析结果', () => {
    const schema = createValidSchema()
    parser.parse(schema)
    const cached = parser.getCached('test-module')
    expect(cached).toBeDefined()
    expect(cached!.identity.id).toBe('test-module')
  })

  it('getCached 对不存在的模块返回 undefined', () => {
    expect(parser.getCached('nonexistent')).toBeUndefined()
  })

  it('clearCache 清空后 getCached 返回 undefined', () => {
    const schema = createValidSchema()
    parser.parse(schema)
    parser.clearCache()
    expect(parser.getCached('test-module')).toBeUndefined()
  })

  it('parseAll 批量解析多个 Schema', () => {
    const schemas = [
      createValidSchema(),
      createValidSchema({
        identity: { ...createValidSchema().identity, id: 'mod2', name: 'Mod2' },
      }),
    ]
    const results = parser.parseAll(schemas)
    expect(results).toHaveLength(2)
    expect(results[0].changed).toBe(true)
    expect(results[1].changed).toBe(true)
  })
})
