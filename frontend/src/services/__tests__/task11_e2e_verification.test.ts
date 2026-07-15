/**
 * Task 11 前端适配 — 端到端功能验证
 *
 * 本文件验证 SchemaDriver 升级 + WebSocket 适配 + 向后兼容的完整功能链路。
 * 设计为 1 条完整用户旅程（7 步串联）+ 2 个补充场景。
 *
 * 用户旅程：插件开发者注册一个 0.2 风格的模块 → SchemaParser 解析 → RenderingEngine 路由 →
 *          SchemaRouter 扩展 → 数据源获取 → WebSocket 消息收发 → 0.1 向后兼容
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { SchemaParser } from '@/services/schema/SchemaParser'
import { RenderingEngine } from '@/services/schema/RenderingEngine'
import { SchemaRouter } from '@/services/schema/SchemaRouter'
import {
  adaptIncomingMessage,
  adaptOutgoingMessage,
  isRustKernelMessage,
} from '@/services/websocket/MessageAdapter'
import type { ModuleUISchema, UIContribution } from '@/types/schema'

// Mock apiClient for datasource tests
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
  },
}))
import apiClient from '@/services/api/client'
import { fetchDynamicDataSource } from '@/services/api/datasource'

// ============================================================================
// 完整用户旅程：7 步串联
// ============================================================================

describe('Task 11 用户旅程：从 Schema 定义到渲染输出', () => {
  // --- 旅程共享状态 ---
  let parser: SchemaParser
  let engine: RenderingEngine
  let router: SchemaRouter
  let testSchema: ModuleUISchema
  let parsedSchema: ReturnType<SchemaParser['parse']>['parsed']
  let renderResult: ReturnType<RenderingEngine['render']>
  let datasourceResponse: { success: boolean; options: Array<{ label: string; value: string }> }
  let wsReceivedType: string

  // --- Step 1: 定义 0.2 Schema 并解析 ui 字段（AC-11-1）---
  it('Step 1 [AC-11-1]: 定义 0.2 Schema，SchemaParser 正确解析 ui.input_form 和 ui.result_widget', () => {
    parser = new SchemaParser()
    router = new SchemaRouter()
    engine = new RenderingEngine({ enableCache: false })

    testSchema = {
      identity: {
        id: 'plugin-advanced-search',
        name: 'Advanced Search',
        version: '2.0.0',
        category: 'extension',
        description: 'Advanced search with dynamic data sources',
        icon: '🔍',
      },
      actions: [
        { id: 'search', name: '搜索', type: 'command' },
      ],
      rendering: {
        chat: [],
        spaces: [],
      },
      clients: {
        requiredSpaces: ['chat', 'workspace', 'floating', 'dock', 'scene'],
        requiredWidgets: [],
      },
      ui: {
        inputForm: {
          fields: [
            { name: 'query', type: 'string', label: '搜索词', required: true },
            {
              name: 'category',
              type: 'select',
              label: '类别',
              datasourceUri: 'datasource://categories/list',
              options: [
                { label: '全部', value: 'all' },
                { label: '技术', value: 'tech' },
              ],
            },
          ],
          submitLabel: '开始搜索',
        },
        resultWidget: {
          type: 'search_results',
          renderSpace: 'workspace',
          props: { pageSize: 20 },
        },
      },
      ui_contributions: [
        { type: 'widget', widgetType: 'search_results', renderSpace: 'workspace', label: '搜索结果' },
        { type: 'panel', widgetType: 'search_filter_panel', renderSpace: 'dock', label: '筛选面板' },
        { type: 'shortcut', widgetType: 'quick_search', renderSpace: 'floating', label: '快捷搜索', order: 1 },
        { type: 'widget', widgetType: 'search_history', renderSpace: 'chat', label: '历史记录' },
        { type: 'widget', widgetType: 'visualization_3d', renderSpace: 'scene', label: '3D 可视化' },
      ],
    }

    const { parsed, changed } = parser.parse(testSchema)
    parsedSchema = parsed

    // 验证 AC-11-1: ui.input_form 解析正确
    expect(parsed.ui).toBeDefined()
    expect(parsed.ui!.inputForm).toBeDefined()
    expect(parsed.ui!.inputForm!.fields).toHaveLength(2)
    expect(parsed.ui!.inputForm!.fields[0].name).toBe('query')
    expect(parsed.ui!.inputForm!.fields[1].datasourceUri).toBe('datasource://categories/list')
    expect(parsed.ui!.inputForm!.submitLabel).toBe('开始搜索')

    // 验证 AC-11-1: ui.result_widget 解析正确
    expect(parsed.ui!.resultWidget).toBeDefined()
    expect(parsed.ui!.resultWidget!.type).toBe('search_results')
    expect(parsed.ui!.resultWidget!.renderSpace).toBe('workspace')
    expect(parsed.ui!.resultWidget!.props).toEqual({ pageSize: 20 })

    expect(changed).toBe(true)
  })

  // --- Step 2: 验证 ui_contributions 解析（AC-11-2）---
  it('Step 2 [AC-11-2]: ParsedSchema.ui_contributions 正确解析所有贡献项', () => {
    expect(parsedSchema.ui_contributions).toBeDefined()
    expect(parsedSchema.ui_contributions).toHaveLength(5)

    // widget 类型
    const widgetContrib = parsedSchema.ui_contributions!.find((c) => c.type === 'widget')
    expect(widgetContrib).toBeDefined()
    expect(widgetContrib!.widgetType).toBe('search_results')
    expect(widgetContrib!.renderSpace).toBe('workspace')

    // panel 类型
    const panelContrib = parsedSchema.ui_contributions!.find((c) => c.type === 'panel')
    expect(panelContrib!.widgetType).toBe('search_filter_panel')
    expect(panelContrib!.renderSpace).toBe('dock')

    // shortcut 类型
    const shortcutContrib = parsedSchema.ui_contributions!.find((c) => c.type === 'shortcut')
    expect(shortcutContrib!.widgetType).toBe('quick_search')
    expect(shortcutContrib!.order).toBe(1)
  })

  // --- Step 3: RenderingEngine 将贡献项路由到正确渲染空间（AC-11-2续 + AC-11-6）---
  it('Step 3 [AC-11-2/AC-11-6]: RenderingEngine.render() 5 渲染空间全覆盖', () => {
    renderResult = engine.render(parsedSchema)

    // AC-11-2: 贡献项路由到正确的渲染空间
    const workspaceInstr = renderResult.bySpace.workspace.find((i) => i.widgetType === 'search_results')
    expect(workspaceInstr).toBeDefined()
    expect(workspaceInstr!.space).toBe('workspace')

    const dockInstr = renderResult.bySpace.dock.find((i) => i.widgetType === 'search_filter_panel')
    expect(dockInstr).toBeDefined()

    const floatingInstr = renderResult.bySpace.floating.find((i) => i.widgetType === 'quick_search')
    expect(floatingInstr).toBeDefined()

    // AC-11-6: 5 个渲染空间均有渲染指令
    expect(renderResult.bySpace.chat.length).toBeGreaterThan(0)
    expect(renderResult.bySpace.workspace.length).toBeGreaterThan(0)
    expect(renderResult.bySpace.floating.length).toBeGreaterThan(0)
    expect(renderResult.bySpace.dock.length).toBeGreaterThan(0)
    expect(renderResult.bySpace.scene.length).toBeGreaterThan(0)

    // all 列表包含 5 个空间
    const spacesInAll = new Set(renderResult.all.map((i) => i.space))
    expect(spacesInAll.has('chat')).toBe(true)
    expect(spacesInAll.has('workspace')).toBe(true)
    expect(spacesInAll.has('floating')).toBe(true)
    expect(spacesInAll.has('dock')).toBe(true)
    expect(spacesInAll.has('scene')).toBe(true)
  })

  // --- Step 4: SchemaRouter 注册自定义路由（AC-11-7）---
  it('Step 4 [AC-11-7]: SchemaRouter.register() + registerFromContributions() 路由正确', () => {
    // register 单个路由
    router.register('custom_widget', 'dock')
    expect(router.resolve('custom_widget')).toBe('dock')

    // registerFromContributions 批量注册
    const contributions: UIContribution[] = [
      { type: 'widget', widgetType: 'custom_a', renderSpace: 'scene' },
      { type: 'panel', widgetType: 'custom_b', renderSpace: 'workspace' },
    ]
    router.registerFromContributions(contributions)
    expect(router.resolve('custom_a')).toBe('scene')
    expect(router.resolve('custom_b')).toBe('workspace')

    // 自定义路由覆盖默认路由
    router.register('review_document', 'floating')
    expect(router.resolve('review_document')).toBe('floating')
  })

  // --- Step 5: 动态数据源获取（AC-11-3）---
  it('Step 5 [AC-11-3]: fetchDynamicDataSource 调用 /api/v1/datasource/{uri}?category=search', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockResolvedValue({
      data: {
        success: true,
        options: [
          { label: '分类A', value: 'cat_a' },
          { label: '分类B', value: 'cat_b' },
        ],
      },
    })

    datasourceResponse = await fetchDynamicDataSource('categories/list', { category: 'search' })

    // 验证请求路径和参数
    expect(mockGet).toHaveBeenCalledWith('/api/v1/datasource/categories/list', {
      params: { category: 'search' },
    })

    // 验证返回的 options 列表
    expect(datasourceResponse.success).toBe(true)
    expect(datasourceResponse.options).toHaveLength(2)
    expect(datasourceResponse.options[0].value).toBe('cat_a')
  })

  // --- Step 6: WebSocket 消息适配（AC-11-4）---
  it('Step 6 [AC-11-4]: adaptIncomingMessage/adaptOutgoingMessage 适配 Rust 内核格式', () => {
    // 适配入站消息：Rust 内核格式
    const rustIncoming = {
      type: 'pipeline_chunk',
      data: {
        thread_id: 'thread-e2e-001',
        content: 'Hello from Rust kernel',
      },
      metadata: { pipeline_id: 'p-e2e', seq: 1 },
    }
    const adaptedIn = adaptIncomingMessage(rustIncoming)

    expect(adaptedIn).not.toBeNull()
    expect(adaptedIn!.type).toBe('pipeline_chunk')
    expect(adaptedIn!.thread_id).toBe('thread-e2e-001')
    expect(adaptedIn!.data.thread_id).toBe('thread-e2e-001')
    expect(adaptedIn!.data.content).toBe('Hello from Rust kernel')
    expect(adaptedIn!.metadata).toEqual({ pipeline_id: 'p-e2e', seq: 1 })
    wsReceivedType = adaptedIn!.type

    // 适配出站消息：前端 → Rust 格式
    const outgoing = {
      type: 'user_input',
      thread_id: 'thread-e2e-001',
      content: '用户输入内容',
    }
    const adaptedOut = adaptOutgoingMessage(outgoing)

    expect(adaptedOut.type).toBe('user_input')
    expect(adaptedOut.data.thread_id).toBe('thread-e2e-001')
    expect(adaptedOut.data.content).toBe('用户输入内容')

    // isRustKernelMessage 识别
    expect(isRustKernelMessage(rustIncoming)).toBe(true)
    expect(isRustKernelMessage(outgoing)).toBe(false)
  })

  // --- Step 7: 向后兼容（AC-11-5）---
  it('Step 7 [AC-11-5]: 0.1 风格 Schema 和扁平 WS 消息向后兼容', () => {
    // 0.1 风格 Schema（含 ui_schema 字段，无 ui/ui_contributions）
    const legacySchema: ModuleUISchema = {
      identity: {
        id: 'legacy-module',
        name: 'Legacy Module',
        version: '1.0.0',
        category: 'builtin',
      },
      actions: [
        { id: 'run', name: '运行', type: 'command' },
      ],
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [{ space: 'workspace', widget: 'table' }],
      },
      clients: {
        requiredSpaces: ['chat', 'workspace'],
        requiredWidgets: ['form'],
      },
      ui_schema: {
        form: {
          fields: [{ name: 'query', type: 'string', label: '查询' }],
        },
      },
    }

    // SchemaParser 不报错
    expect(() => parser.parse(legacySchema)).not.toThrow()

    const { parsed } = parser.parse(legacySchema)
    // 旧字段正常解析
    expect(parsed.rendering.chat).toHaveLength(1)
    expect(parsed.rendering.chat[0].type).toBe('form')
    expect(parsed.rendering.spaces[0].space).toBe('workspace')
    // 新字段为 undefined（渐进增强）
    expect(parsed.ui).toBeUndefined()
    expect(parsed.ui_contributions).toBeUndefined()

    // 0.1 Python 格式的扁平 WS 消息
    const legacyWs = {
      type: 'pipeline_chunk',
      thread_id: 't-legacy-001',
      content: 'Legacy flat message',
      pipeline_id: 'p-old',
    }
    const adapted = adaptIncomingMessage(legacyWs)

    expect(adapted).not.toBeNull()
    expect(adapted!.type).toBe('pipeline_chunk')
    expect(adapted!.thread_id).toBe('t-legacy-001')
    expect(adapted!.data.pipeline_id).toBe('p-old')
    // 扁平格式不应被识别为 Rust 内核格式
    expect(isRustKernelMessage(legacyWs)).toBe(false)

    // 验证旅程中 step 6 的状态正确传递到了这里
    expect(wsReceivedType).toBe('pipeline_chunk')
  })
})

// ============================================================================
// 补充场景 1：错误输入（无效 Schema）
// ============================================================================

describe('补充场景 1：错误输入验证', () => {
  let parser: SchemaParser

  beforeEach(() => {
    parser = new SchemaParser()
  })

  it('缺少 identity 字段的 Schema 抛出 SchemaParseError', () => {
    const invalidSchema = {
      actions: [],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [], requiredWidgets: [] },
    } as any

    expect(() => parser.parse(invalidSchema)).toThrow()
  })

  it('缺少 actions 字段的 Schema 验证失败', () => {
    const invalidSchema = {
      identity: { id: 'test', name: 'Test', version: '1.0', category: 'builtin' },
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: [], requiredWidgets: [] },
    } as any

    expect(() => parser.parse(invalidSchema)).toThrow()
  })

  it('无 type 的 WS 消息返回 null', () => {
    const adapted = adaptIncomingMessage({ data: { foo: 'bar' } } as any)
    expect(adapted).toBeNull()
  })

  it('空对象作为 WS 消息返回 null', () => {
    const adapted = adaptIncomingMessage({} as any)
    expect(adapted).toBeNull()
  })

  it('fetchDynamicDataSource 网络错误时抛出异常', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockRejectedValue(new Error('Network timeout'))

    await expect(fetchDynamicDataSource('any/source')).rejects.toThrow('Network timeout')
  })
})

// ============================================================================
// 补充场景 2：边界场景（空数据 / fallback / 增量更新）
// ============================================================================

describe('补充场景 2：边界场景验证', () => {
  it('空 ui_contributions 的 Schema 渲染时不产生贡献指令', () => {
    const parser = new SchemaParser()
    const engine = new RenderingEngine({ enableCache: false })

    const schema: ModuleUISchema = {
      identity: { id: 'empty-contrib', name: 'Empty', version: '1.0', category: 'builtin' },
      actions: [],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: ['chat'], requiredWidgets: [] },
    }

    const { parsed } = parser.parse(schema)
    const result = engine.render(parsed)

    expect(result.all.length).toBe(0)
  })

  it('未知 widget_type 的 SchemaRouter resolve 返回 fallback chat', () => {
    const router = new SchemaRouter()
    expect(router.resolve('nonexistent_widget_xyz')).toBe('chat')
  })

  it('SchemaParser 增量更新：相同 Schema 第二次解析 changed=false', () => {
    const parser = new SchemaParser()
    const schema: ModuleUISchema = {
      identity: { id: 'inc-test', name: 'Inc', version: '1.0', category: 'builtin' },
      actions: [{ id: 'a', name: 'A', type: 'command' }],
      rendering: { chat: [], spaces: [] },
      clients: { requiredSpaces: ['chat'], requiredWidgets: [] },
      ui: {
        inputForm: {
          fields: [{ name: 'f', type: 'string', label: 'F' }],
        },
      },
    }

    const { changed: first } = parser.parse(schema)
    expect(first).toBe(true)

    const { changed: second } = parser.parse(schema)
    expect(second).toBe(false)
  })

  it('adaptOutgoingMessage 心跳消息保持正确格式', () => {
    const heartbeat = { type: 'heartbeat', timestamp: 999 }
    const adapted = adaptOutgoingMessage(heartbeat)

    expect(adapted.type).toBe('heartbeat')
    expect(adapted.data.timestamp).toBe(999)
  })

  it('RenderingEngine 缓存机制：相同 versionHash 返回缓存结果', () => {
    const engine = new RenderingEngine({ enableCache: true })
    const parser = new SchemaParser()

    const schema: ModuleUISchema = {
      identity: { id: 'cache-test', name: 'Cache', version: '1.0', category: 'builtin' },
      actions: [],
      rendering: {
        chat: [{ type: 'form' }],
        spaces: [],
      },
      clients: { requiredSpaces: ['chat'], requiredWidgets: [] },
    }

    const { parsed } = parser.parse(schema)
    const result1 = engine.render(parsed)
    const result2 = engine.render(parsed)

    // 相同 versionHash → 返回同一引用（缓存命中）
    expect(result1).toBe(result2)
  })
})
