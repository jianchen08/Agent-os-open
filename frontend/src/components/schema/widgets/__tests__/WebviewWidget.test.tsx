/**
 * WebviewWidget 上行消息路由测试 — 阶段 3 前端部分
 *
 * 背景：
 * - iframe（srcDoc + sandbox 不开 allow-same-origin）经 BOOTSTRAP_JS 注入
 *   `window.agentos.postMessage(method, params)`，插件调用后上行到宿主。
 * - 宿主经 `validateWebviewEvent`（origin='null' + __agentos_webview 魔数）校验
 *   后，按 method 路由到后端，再把 result/error 下行推回 iframe。
 *
 * 验证（可观察行为）：
 * - AC-1: action 方法（如 'demo.ping'）→ POST /api/v1/actions/execute { action, args }
 * - AC-2: REST 方法（'/...' 开头）+ params → POST msg.method with params
 * - AC-3: REST 方法（'/...' 开头）+ 无 params → GET msg.method
 * - AC-4: action 成功响应 → iframe 收到 `${method}.result` 下行（id 对应）
 * - AC-5: action 失败响应 → iframe 收到 `${method}.error` 下行（带 message）
 * - AC-6: __ready 不走 apiClient（只走 info 日志）
 */
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'

import { apiClient } from '@/services/api/client'

// ── Mock 外部依赖 ──
vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock
const apiPost = apiClient.post as unknown as Mock

/** 从 iframe srcdoc 里读宿主注入的实例令牌（bootstrap 变量 TOKEN）。 */
function getIframeToken(): string {
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  const src = iframe.getAttribute('srcdoc') ?? ''
  const m = src.match(/TOKEN = "([^"]+)"/)
  if (!m) throw new Error('iframe srcdoc 中未找到实例令牌 TOKEN')
  return m[1]
}

/** 模拟 iframe 上行：发一条带实例令牌的合法 postMessage 事件（origin='null' + 魔数）。 */
function postUp(method: string, params?: unknown, id = 'wv_1'): void {
  const data: Record<string, unknown> = {
    __agentos_webview: true,
    __wv_token: getIframeToken(),
    id,
    method,
  }
  if (params !== undefined) data.params = params
  window.dispatchEvent(new MessageEvent('message', { origin: 'null', data }))
}

/** 模拟"无令牌/伪造令牌"的上行（安全审查 2026-08-20 B-4 负例）。 */
function postUpForged(method: string, token: string, params?: unknown, id = 'wv_1'): void {
  const data: Record<string, unknown> = { __agentos_webview: true, __wv_token: token, id, method }
  if (params !== undefined) data.params = params
  window.dispatchEvent(new MessageEvent('message', { origin: 'null', data }))
}

/** 从 contentWindow.postMessage 调用记录里找指定 method 的下行消息。 */
function findDownMessage(spy: Mock, method: string): Record<string, unknown> | undefined {
  for (const call of spy.mock.calls) {
    const msg = call[0] as Record<string, unknown> | undefined
    if (msg && typeof msg === 'object' && msg.method === method) return msg
  }
  return undefined
}

describe('WebviewWidget — 上行消息路由', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 默认 HTML 加载成功（保证 iframe 挂载 + handler 注册）
    apiGet.mockResolvedValue({ data: '<html><body></body></html>' })
  })
  afterEach(() => vi.clearAllMocks())

  it('AC-1: action 方法 → POST /api/v1/actions/execute { action, args }', async () => {
    apiPost.mockResolvedValue({ data: { ok: true } })
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('demo.ping', { ts: 123 }, 'wv_1')

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/api/v1/actions/execute', {
        action: 'demo.ping',
        args: { ts: 123 },
      })
    })
  })

  it('AC-2: REST 方法 (本插件 /ext/{pluginId}/ + params) → POST msg.method with params', async () => {
    apiPost.mockResolvedValue({ data: { ok: true } })
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('/ext/demo/foo', { a: 1 })

    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith('/ext/demo/foo', { a: 1 })
    })
  })

  it('AC-3: REST 方法 (本插件 /ext/{pluginId}/ + 无 params) → GET msg.method', async () => {
    apiGet.mockResolvedValue({ data: { items: [] } })
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('/ext/demo/items')

    await waitFor(() => {
      // 排除 HTML 加载那次 get（/ext/demo/webview）
      const calls = apiGet.mock.calls.filter((c) => c[0] === '/ext/demo/items')
      expect(calls).toHaveLength(1)
    })
  })

  it('AC-7: REST 方法越出本插件路由白名单 → 拒绝且不调 apiClient（B-4）', async () => {
    apiPost.mockClear()
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    // 曾可直接借 Bearer 调任意内核端点（如 /api/v1/xxx / 他插件 /ext/other/...）
    postUp('/api/v1/admin/pipelines', { x: 1 })

    await waitFor(() => {
      const msg = findDownMessage(downSpy, '/api/v1/admin/pipelines.error')
      expect(msg).toBeDefined()
      expect(String((msg?.params as { message?: string })?.message ?? '')).toContain('不在本插件路由白名单')
    })
    expect(apiPost).not.toHaveBeenCalledWith('/api/v1/admin/pipelines', { x: 1 })
    expect(apiPost).toHaveBeenCalledTimes(0)

    // 他插件的 /ext/ 前缀同样拒绝
    postUp('/ext/otherapp/exec', { y: 2 })
    await waitFor(() => {
      const msg2 = findDownMessage(downSpy, '/ext/otherapp/exec.error')
      expect(msg2).toBeDefined()
      const msg = msg2 as { params?: { message?: string } } | undefined
      expect(String(msg?.params?.message ?? '')).toContain('不在本插件路由白名单')
    })
    expect(apiPost).toHaveBeenCalledTimes(0)
  })

  it('AC-8: 伪造/缺失实例令牌的消息一律丢弃（B-4）', async () => {
    apiPost.mockClear()
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    // 伪造令牌（其它页面可发的 origin='null' 消息）
    postUpForged('demo.ping', 'forged-token', { ts: 1 })
    // 缺令牌
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'null',
        data: { __agentos_webview: true, method: 'demo.ping', params: { ts: 1 } },
      }),
    )

    await new Promise((r) => setTimeout(r, 50))
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPost).toHaveBeenCalledTimes(0)
  })

  it('AC-4: action 成功响应 → iframe 收到 method.result 下行（id 对应）', async () => {
    apiPost.mockResolvedValue({ data: { pong: true } })
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const contentWindow = iframe.contentWindow
    expect(contentWindow).not.toBeNull()
    const downSpy = vi.spyOn(contentWindow!, 'postMessage')

    postUp('demo.ping', { ts: 1 }, 'wv_xyz')

    await waitFor(() => {
      const msg = findDownMessage(downSpy, 'demo.ping.result')
      expect(msg).toBeDefined()
      expect(msg?.__agentos_webview).toBe(true)
      expect(msg?.id).toBe('wv_xyz')
      expect(msg?.params).toEqual({ pong: true })
    })
    // 确认 origin 用 '*'（sandbox iframe origin='null'）
    expect(downSpy.mock.calls.at(-1)?.[1]).toBe('*')
  })

  it('AC-5: action 失败响应 → iframe 收到 method.error 下行（带 message）', async () => {
    apiPost.mockRejectedValue(new Error('boom'))
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const contentWindow = iframe.contentWindow
    expect(contentWindow).not.toBeNull()
    const downSpy = vi.spyOn(contentWindow!, 'postMessage')

    postUp('demo.fail', {}, 'wv_err')

    await waitFor(() => {
      const msg = findDownMessage(downSpy, 'demo.fail.error')
      expect(msg).toBeDefined()
      expect(msg?.id).toBe('wv_err')
      expect(msg?.params).toMatchObject({ message: 'boom' })
    })
  })

  it('AC-6: __ready 不调 apiClient（只走 info 日志）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    apiPost.mockClear()
    postUp('__ready', {})

    // 给微任务一段缓冲，确认不会异步触发 apiClient
    await new Promise((r) => setTimeout(r, 50))
    expect(apiPost).not.toHaveBeenCalled()
  })
})
