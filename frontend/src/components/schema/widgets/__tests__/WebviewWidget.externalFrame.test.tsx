/** @feature FP-T12 前端适配 | @ci: frontend-test */

/**
 * WebviewWidget 外部站点内嵌形态（externalFramePath）
 *
 * 契约（可观察行为）：
 * - 就绪前：显示启动占位，**不**渲染 iframe（不拿空地址挂一个会白屏的框）
 * - 端点返回 available + http(s) URL → 渲染 iframe，且 `src` 等于该 URL、
 *   **不带 sandbox**（官方站点需要同源脚本 / cookie / WebSocket；sandbox 会把
 *   它降成 opaque origin，认证态与网关请求全废）
 * - 外部帧形态不取插件 HTML（不走 htmlPath 的 srcDoc 链路）
 * - 端点给出 reason（未就绪且非 starting）→ 错误态展示该原因 + 重试入口；
 *   点重试重新探测，成功后就绪
 * - 非 http(s) URL → 不渲染 iframe（scheme 白名单）
 * - 轮询：starting 期间持续探测，ready 后停止
 *
 * mock 仅用于外部依赖（axios apiClient = 网络），不用零延迟假定时：轮询间隔用
 * fake clock 推进，断言「未到间隔不重复请求 / 推进后拿到就绪地址」。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { WebviewWidget, normalizeExternalFrameUrl } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock

const FRAME_PATH = '/api/dsh/official-ui?ensure=1'
const OFFICIAL_URL = 'http://127.0.0.1:41234/?token=abc'

describe('normalizeExternalFrameUrl', () => {
  it('只放行 http(s) 绝对 URL，其余一律 null', () => {
    expect(normalizeExternalFrameUrl('http://127.0.0.1:1/?token=x')).toBe('http://127.0.0.1:1/?token=x')
    expect(normalizeExternalFrameUrl('https://example.test/a')).toBe('https://example.test/a')
    expect(normalizeExternalFrameUrl('javascript:alert(1)')).toBeNull()
    expect(normalizeExternalFrameUrl('data:text/html,<b>x</b>')).toBeNull()
    expect(normalizeExternalFrameUrl('/relative')).toBeNull()
    expect(normalizeExternalFrameUrl('')).toBeNull()
    expect(normalizeExternalFrameUrl(null)).toBeNull()
    expect(normalizeExternalFrameUrl(42)).toBeNull()
  })
})

describe('WebviewWidget — 外部站点内嵌（externalFramePath）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  it('就绪即渲染 iframe（src = 端点下发 URL，无 sandbox），且不取插件 HTML', async () => {
    apiGet.mockResolvedValue({
      data: { available: true, url: OFFICIAL_URL, starting: false, reason: null },
    })

    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    const frame = (await screen.findByTitle('Webview')) as HTMLIFrameElement
    expect(frame.getAttribute('src')).toBe(OFFICIAL_URL)
    // 顶层文档语义：不带 sandbox、不用 srcDoc 注入
    expect(frame.hasAttribute('sandbox')).toBe(false)
    expect(frame.hasAttribute('srcdoc')).toBe(false)
    // 外部帧形态只探端点，不取 htmlPath 的 HTML
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(String(apiGet.mock.calls[0][0])).toContain(FRAME_PATH)
    expect(String(apiGet.mock.calls[0][0])).not.toContain('/webview')
  })

  it('starting 期间按间隔轮询（未到间隔不重复请求），就绪后发起 iframe', async () => {
    // 时序不变量用真实计时验证：外部应用冷启动是真实等待，轮询间隔必须真的存在
    // （轮询间隔 2s；500ms 内重复请求即视为间隔失效）。
    apiGet.mockResolvedValueOnce({ data: { available: false, starting: true, reason: null } })
    apiGet.mockResolvedValue({
      data: { available: true, url: OFFICIAL_URL, starting: false, reason: null },
    })

    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    // 首次探测已发；未就绪 → 不渲染 iframe（不留空 src 框）
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(1))
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()

    // 间隔未到：不得重复请求（时序不变量）
    await new Promise((resolve) => setTimeout(resolve, 500))
    expect(apiGet).toHaveBeenCalledTimes(1)

    // 间隔到后拿到就绪地址 → iframe 出现
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument(), {
      timeout: 6_000,
    })
    expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(2)
    expect((screen.getByTitle('Webview') as HTMLIFrameElement).getAttribute('src')).toBe(OFFICIAL_URL)
  })

  it('端点给 reason → 错误态展示原因 + 重试；重试就绪后渲染 iframe', async () => {
    apiGet.mockResolvedValueOnce({
      data: { available: false, starting: false, reason: '未找到 Node >= 22', url: null },
    })
    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    expect(await screen.findByText(/外部页面不可用/)).toBeInTheDocument()
    expect(screen.getByText(/未找到 Node >= 22/)).toBeInTheDocument()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()

    apiGet.mockResolvedValueOnce({
      data: { available: true, url: OFFICIAL_URL, starting: false, reason: null },
    })
    fireEvent.click(screen.getByRole('button', { name: '重试' }))

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect((screen.getByTitle('Webview') as HTMLIFrameElement).getAttribute('src')).toBe(OFFICIAL_URL)
  })

  it('端点给非 http(s) URL → 落显式错误，不渲染 iframe（scheme 白名单）', async () => {
    apiGet.mockResolvedValue({
      data: { available: true, url: 'javascript:alert(1)', starting: false, reason: null },
    })
    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    expect(await screen.findByText(/外部页面不可用/)).toBeInTheDocument()
    expect(screen.getByText(/仅支持 http\/https 绝对 URL/)).toBeInTheDocument()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()
    // 就绪但地址非法 = 声明面错误：不静默重试（只探测一次）
    expect(apiGet).toHaveBeenCalledTimes(1)
  })
})

describe('WebviewWidget — 外部站点探测异常面', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('探测请求抛错（Error 形态）→ 错误态展示异常消息，不渲染 iframe', async () => {
    apiGet.mockRejectedValue(new Error('网络断开'))
    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    expect(await screen.findByText(/外部页面不可用/)).toBeInTheDocument()
    expect(screen.getByText(/网络断开/)).toBeInTheDocument()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()
    // 异常即终态：不静默重试
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('探测抛非 Error 值 → 落兜底文案「外部页面探测失败」', async () => {
    apiGet.mockRejectedValue('boom')
    render(<WebviewWidget pluginId="dsh_adapter" externalFramePath={FRAME_PATH} />)

    expect(await screen.findByText(/外部页面探测失败/)).toBeInTheDocument()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()
  })
})
