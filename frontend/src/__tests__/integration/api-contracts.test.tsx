/**
 * 前后端契约对齐测试（Contract Tests）
 *
 * 目的：验证「前端服务层/组件实际发出的 HTTP 请求」与「后端端点签名」一致。
 * 介于纯单元 mock（弱：只验证 mock 被调用）与 e2e（重：需起后端）之间。
 *
 * 方法论（关键）：
 * - **不 vi.mock 网络层模块**：导入真实的 apiClient（axios 单例），服务模块拿到的是同一个实例。
 * - 仅用 `vi.spyOn(apiClient, 'get'/'post'/'put')` 在方法边界拦截：阻止真实网络请求 + 捕获调用参数。
 * - 被测对象全部用真实代码：agents.ts / GrowthLoop.ts / commandDispatcher / WebviewWidget 均不 mock。
 * - 断言三要素：HTTP method、URL（含路径参数插值）、请求体 shape。
 *
 * 与单元 mock 测试的区别：
 * - 单元 mock（如 agents.test.ts）vi.mock 整个 client 模块 → 服务拿到的是假 client；
 *   若服务误导入别的 client，单测发现不了。本测试用真实 client + spyOn，验证真实接线。
 *
 * 覆盖的 5 条契约（对应 6 条前端数据路径中的上行/读写路径）：
 *   C1. getAgentSchema()        → GET    /ext/agent_manager/agents/schema
 *   C2. putAgentConfig(id,yaml) → PUT    /ext/agent_manager/agents/{id}/config   body { yaml }
 *   C3. commandDispatcher transport → POST /api/v1/actions/execute    body { action, args }
 *   C4. WebviewWidget HTML 加载 → GET    /ext/{pluginId}{path}        (默认 path=/webview)
 *   C5. WebviewWidget action 上行 → POST /api/v1/actions/execute      body { action: method, args: params }
 *
 * 后端端点定义（kernel/crates/api/src/server.rs）：
 *   - GET  /ext/agent_manager/agents/schema         (line 95)
 *   - GET|PUT /ext/agent_manager/agents/{id}/config (line 96-99)
 *   - POST /api/v1/actions/execute       (line 102)
 *   - /ext/{*rest} 通配                  (http_dispatcher.rs:197)
 */

import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

// 真实 apiClient（axios 单例）—— 服务模块导入的是同一个实例，spy 在此对象上即生效。
import apiClient from '@/services/api/client'

// 真实服务模块（不 mock）
import { getAgentSchema, putAgentConfig } from '@/services/api/agents'
import { initializeGrowthLoop } from '@/services/modules/GrowthLoop'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'

// ── spyOn 工具：在每个用例前重置 ──
let getSpy: Mock
let postSpy: Mock
let putSpy: Mock

beforeEach(() => {
  getSpy = vi.spyOn(apiClient, 'get').mockResolvedValue({ data: {} }) as unknown as Mock
  postSpy = vi.spyOn(apiClient, 'post').mockResolvedValue({ data: {} }) as unknown as Mock
  putSpy = vi.spyOn(apiClient, 'put').mockResolvedValue({ data: {} }) as unknown as Mock
})

afterEach(() => {
  vi.restoreAllMocks()
  // commandDispatcher 的 transport 在 GrowthLoop 测试中被注入，清理避免串扰
  contributionRegistry.clear()
})

// ============================================================================
// C1: getAgentSchema → GET /ext/agent_manager/agents/schema
// ============================================================================

describe('C1 契约: getAgentSchema() → GET /ext/agent_manager/agents/schema', () => {
  it('调用 getAgentSchema 应发 GET /ext/agent_manager/agents/schema（method + url 对齐后端）', async () => {
    getSpy.mockResolvedValue({ data: { fields: [{ name: 'config_id', type: 'string' }] } })

    await getAgentSchema()

    expect(getSpy).toHaveBeenCalledTimes(1)
    const [url, config] = getSpy.mock.calls[0]
    expect(url).toBe('/ext/agent_manager/agents/schema')
    // 不应附带 body（GET）
    expect(config).toBeUndefined()
  })

  it('响应应含 fields 数组（与后端 agents_schema_handler 返回 shape 对齐）', async () => {
    getSpy.mockResolvedValue({
      data: { fields: [{ name: 'name', type: 'string', label: '名称', required: true }] },
    })

    const result = await getAgentSchema()

    expect(Array.isArray(result.fields)).toBe(true)
  })
})

// ============================================================================
// C2: putAgentConfig(id, yaml) → PUT /ext/agent_manager/agents/{id}/config body { yaml }
// ============================================================================

describe('C2 契约: putAgentConfig(id, yaml) → PUT /ext/agent_manager/agents/{id}/config', () => {
  it('应发 PUT /ext/agent_manager/agents/{id}/config，body 为 { yaml }（路径插值 + body shape 对齐）', async () => {
    putSpy.mockResolvedValue({ data: { config_id: 'my-agent', success: true, backup: 'my-agent.yaml.bak' } })

    const yamlContent = 'name: 测试\nmodel: gpt-4\n'
    await putAgentConfig('my-agent', yamlContent)

    expect(putSpy).toHaveBeenCalledTimes(1)
    const [url, body] = putSpy.mock.calls[0]
    // 路径参数插值正确
    expect(url).toBe('/ext/agent_manager/agents/my-agent/config')
    // body shape: 后端 AgentConfigUpdateRequest { yaml: String }
    expect(body).toEqual({ yaml: yamlContent })
  })

  it('agentId 含特殊字符时应原样进入路径段（不做额外编码/拼接）', async () => {
    putSpy.mockResolvedValue({ data: { config_id: 'a.b-c', success: true } })

    await putAgentConfig('a.b-c', 'k: v')

    expect(putSpy.mock.calls[0][0]).toBe('/ext/agent_manager/agents/a.b-c/config')
  })
})

// ============================================================================
// C3: commandDispatcher transport → POST /api/v1/actions/execute body { action, args }
//     （真实 GrowthLoop 注入 + 真实 commandDispatcher，仅 apiClient.post 被 spy）
// ============================================================================

describe('C3 契约: GrowthLoop 注入的 transport → POST /api/v1/actions/execute', () => {
  beforeEach(() => {
    // initializeGrowthLoop 会调 getSchema（→ apiClient.get spied）+ loadFromSchema；
    // 提供最小合法 schema 避免下游处理报错。
    getSpy.mockResolvedValue({
      data: {
        agents: [],
        pipelines: [],
        tools: [],
        routes: {},
        plugin_configs: [],
        plugin_contributes: [],
      },
    })
  })

  it('initializeGrowthLoop 后 executeCommand 应经 transport 调 POST /api/v1/actions/execute', async () => {
    await initializeGrowthLoop()

    await commandDispatcher.executeCommand('cost.showReport', { metric: 'tokens' })

    const actionCalls = postSpy.mock.calls.filter((c) => c[0] === '/api/v1/actions/execute')
    expect(actionCalls).toHaveLength(1)
    const [, body] = actionCalls[0]
    // body shape: 后端 ActionsExecuteRequest { action: String, args: Value }
    expect(body).toEqual({ action: 'cost.showReport', args: { metric: 'tokens' } })
  })

  it('无 args 时 body.args 应为 undefined（对齐后端 #[serde(default)]）', async () => {
    await initializeGrowthLoop()

    await commandDispatcher.executeCommand('plugin.run')

    const actionCalls = postSpy.mock.calls.filter((c) => c[0] === '/api/v1/actions/execute')
    expect(actionCalls[0][1]).toEqual({ action: 'plugin.run', args: undefined })
  })
})

// ============================================================================
// C4: WebviewWidget HTML 加载 → GET /ext/{pluginId}{path}
// ============================================================================

describe('C4 契约: WebviewWidget HTML 加载 → GET /ext/{pluginId}{path}', () => {
  beforeEach(() => {
    // HTML 加载用 text 响应
    getSpy.mockResolvedValue({ data: '<html><body></body></html>' })
  })

  it('默认 htmlPath 时应 GET /ext/{pluginId}/webview', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        '/ext/demo/webview',
        expect.objectContaining({ responseType: 'text' }),
      )
    })
  })

  it('htmlPath 为相对路径时应拼成 /ext/{pluginId}/{path}', async () => {
    render(<WebviewWidget pluginId="demo" htmlPath="editor" widgetId="w2" />)

    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        '/ext/demo/editor',
        expect.anything(),
      )
    })
  })

  it('htmlPath 以 / 开头时应拼成 /ext/{pluginId}{path}（不重复斜杠）', async () => {
    render(<WebviewWidget pluginId="demo" htmlPath="/editor/index.html" widgetId="w3" />)

    await waitFor(() => {
      expect(getSpy).toHaveBeenCalledWith(
        '/ext/demo/editor/index.html',
        expect.anything(),
      )
    })
  })
})

// ============================================================================
// C5: WebviewWidget action 上行 → POST /api/v1/actions/execute body { action: method, args: params }
// ============================================================================

describe('C5 契约: WebviewWidget action 上行 → POST /api/v1/actions/execute', () => {
  beforeEach(() => {
    getSpy.mockResolvedValue({ data: '<html><body></body></html>' })
    postSpy.mockResolvedValue({ data: { ok: true } })
  })

  /** 模拟 iframe 上行：发合法 postMessage（origin='null' + 魔数）。 */
  function postUp(method: string, params?: unknown, id = 'wv_1'): void {
    const data: Record<string, unknown> = { __agentos_webview: true, id, method }
    if (params !== undefined) data.params = params
    window.dispatchEvent(new MessageEvent('message', { origin: 'null', data }))
  }

  it('action 方法 → POST /api/v1/actions/execute { action: method, args: params }', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('demo.ping', { ts: 123 })

    await waitFor(() => {
      const actionCalls = postSpy.mock.calls.filter((c) => c[0] === '/api/v1/actions/execute')
      expect(actionCalls).toHaveLength(1)
      // body shape: action = method 字符串, args = params 对象
      expect(actionCalls[0][1]).toEqual({ action: 'demo.ping', args: { ts: 123 } })
    })
  })

  it('REST 方法 (/开头) + params → POST 该路径 with params（不走 actions/execute）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w2" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('/api/v1/foo', { a: 1 })

    await waitFor(() => {
      expect(postSpy).toHaveBeenCalledWith('/api/v1/foo', { a: 1 })
      // 不应误进 actions/execute
      const actionCalls = postSpy.mock.calls.filter((c) => c[0] === '/api/v1/actions/execute')
      expect(actionCalls).toHaveLength(0)
    })
  })

  it('REST 方法 (/开头) + 无 params → GET 该路径（区分读/写）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w3" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('/api/v1/items')

    await waitFor(() => {
      const itemCalls = getSpy.mock.calls.filter((c) => c[0] === '/api/v1/items')
      expect(itemCalls).toHaveLength(1)
    })
  })
})
