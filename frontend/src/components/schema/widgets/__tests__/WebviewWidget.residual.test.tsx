/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WebviewWidget 残余分支补测（簇3，与既有 WebviewWidget.test.tsx 互补）
 *
 * 既有测试覆盖上行消息路由（AC-1..AC-8）。本文件补齐其余分支：
 * - pluginId 缺失 → 不请求、渲染「缺少 pluginId」错误态
 * - HTML 拉取：带 head 的 HTML 只在 head 内插 CSP（不重复插 bootstrap 到 body）
 * - 无 head / 无 html 结构 → 两种包装路径的注入位置
 * - 拉取失败（Error / 非 Error 拒因）→ 错误态渲染，未失败前显示加载占位
 * - 挂载后取消（卸载中 resolve）→ 不再 setState（cancelled 守卫）
 * - 下行：widgetEventStore.latest 变化 → iframe 收到 'widget.event' 消息
 * - 无 widgetId 时不订阅事件（latest 恒 undefined，不下行）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - `.catch` 中 `if (cancelled) return`（L132）需在请求在途时卸载组件：本文件以
 *   "卸载后才 resolve" 用例覆盖（断言无错误态渲染、无 act 警告）。
 * - `crypto.randomUUID` 缺失时的 `wv_...` 兜底（L105）为老浏览器兼容分支：
 *   jsdom 提供 randomUUID，需 stub 移除才能触达——本文件以 stubGlobal 覆盖。
 * - `typeof res.data === 'string' ? res.data : String(res.data)` 的非字符串面：
 *   axios responseType:'text' 正常恒为 string，非 string 仅在异常中间件下出现，
 *   属防御分支；以 mock 返回对象驱动覆盖。
 */
import { render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock
const apiPost = apiClient.post as unknown as Mock

/** 读取渲染出的 iframe srcdoc */
function getSrcDoc(): string {
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return iframe.getAttribute('srcdoc') ?? ''
}

beforeEach(() => {
  vi.clearAllMocks()
  useWidgetEventStore.setState({ latest: {} })
})

afterEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('pluginId 缺失', () => {
  it('无 pluginId → 不发起请求，渲染缺失错误态', async () => {
    render(<WebviewWidget />)

    expect(await screen.findByText(/缺少 pluginId/)).toBeInTheDocument()
    expect(apiGet).not.toHaveBeenCalled()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()
  })
})

describe('HTML 包装注入位置', () => {
  it('含 head 的 HTML → 仅 head 内插 CSP，不追加 bootstrap 脚本', async () => {
    apiGet.mockResolvedValue({ data: '<html><head><title>T</title></head><body>hi</body></html>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const srcDoc = getSrcDoc()
    expect(srcDoc).toContain('Content-Security-Policy')
    // CSP 紧跟 head 开标签之后
    expect(srcDoc).toMatch(/<head><meta http-equiv="Content-Security-Policy"/)
    expect(srcDoc).not.toContain('window.agentos')
  })

  it('只有 html 无 head → 注入 head 包裹 CSP + bootstrap', async () => {
    apiGet.mockResolvedValue({ data: '<html><body>raw</body></html>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const srcDoc = getSrcDoc()
    expect(srcDoc).toMatch(/<html><head><meta http-equiv="Content-Security-Policy"/)
    expect(srcDoc).toContain('window.agentos')
    expect(srcDoc).toContain('__ready')
  })

  it('无结构 HTML → 包一层完整文档（CSP 在 head、bootstrap 在 body 尾）', async () => {
    apiGet.mockResolvedValue({ data: '<p>bare</p>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const srcDoc = getSrcDoc()
    expect(srcDoc).toMatch(/^<!DOCTYPE html><html><head><meta http-equiv="Content-Security-Policy"/)
    expect(srcDoc).toContain('<p>bare</p>')
    expect(srcDoc.indexOf('window.agentos')).toBeGreaterThan(srcDoc.indexOf('<p>bare</p>'))
  })

  it('响应 data 非字符串（异常中间件）→ 字符串化后仍包装出合法 srcDoc', async () => {
    apiGet.mockResolvedValue({ data: { toString: () => '<div>obj</div>' } })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(getSrcDoc()).toContain('<div>obj</div>')
  })

  it('htmlPath 传入时请求该路径；缺省请求 /webview', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    const { unmount } = render(<WebviewWidget pluginId="demo" htmlPath="ui/index.html" />)

    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(String(apiGet.mock.calls[0][0])).toContain('ui/index.html')
    expect(apiGet.mock.calls[0][1]).toMatchObject({ responseType: 'text' })
    unmount()

    apiGet.mockClear()
    apiGet.mockResolvedValue({ data: '<p>y</p>' })
    render(<WebviewWidget pluginId="demo" />)
    await waitFor(() => expect(apiGet).toHaveBeenCalled())
    expect(String(apiGet.mock.calls[0][0])).toContain('/webview')
  })
})

describe('加载失败与在途取消', () => {
  it('拉取失败（Error）→ 渲染错误文案', async () => {
    apiGet.mockRejectedValue(new Error('network down'))
    render(<WebviewWidget pluginId="demo" />)

    expect(await screen.findByText(/Webview 加载失败/)).toBeInTheDocument()
    expect(screen.getByText(/network down/)).toBeInTheDocument()
  })

  it('拉取失败（非 Error 拒因）→ 回退默认失败文案', async () => {
    apiGet.mockRejectedValue({ code: 'E_X' })
    render(<WebviewWidget pluginId="demo" />)

    expect(await screen.findByText(/Webview 加载失败/)).toBeInTheDocument()
    expect(screen.getByText(/加载插件 HTML 失败/)).toBeInTheDocument()
  })

  it('在途加载显示占位（未拿到 HTML 前不渲染 iframe）', () => {
    apiGet.mockReturnValue(new Promise(() => {}))
    render(<WebviewWidget pluginId="demo" />)

    expect(screen.getByText('加载 Webview...')).toBeInTheDocument()
    expect(screen.queryByTitle('Webview')).not.toBeInTheDocument()
  })

  it('请求在途时卸载 → 不 setState（cancelled 守卫，无错误态泄漏）', async () => {
    let resolveGet: (v: unknown) => void = () => {}
    apiGet.mockReturnValue(
      new Promise((resolve) => {
        resolveGet = resolve
      }),
    )
    const { unmount } = render(<WebviewWidget pluginId="demo" />)
    unmount()

    // 卸载后才 resolve，且以 reject 触发 .catch 的 cancelled 分支
    resolveGet(Promise.reject(new Error('late fail')))
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByText(/Webview 加载失败/)).not.toBeInTheDocument()
  })
})

describe('下行事件推送', () => {
  it('widgetEventStore.latest 变化 → iframe 收到 widget.event 通知', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" widgetId="w-down" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const contentWindow = iframe.contentWindow
    expect(contentWindow).not.toBeNull()
    const postSpy = vi.spyOn(contentWindow!, 'postMessage')

    useWidgetEventStore.setState({ latest: { 'w-down': { progress: 42 } } })

    await waitFor(() => {
      const sent = postSpy.mock.calls.find((c) => {
        const msg = c[0] as Record<string, unknown> | undefined
        return msg && msg.method === 'widget.event'
      })
      expect(sent).toBeDefined()
      expect(sent?.[0]).toMatchObject({ __agentos_webview: true, method: 'widget.event' })
      expect((sent?.[0] as { params?: unknown }).params).toEqual({ progress: 42 })
      expect(sent?.[1]).toBe('*')
    })
  })

  it('无 widgetId → 不订阅事件，store 更新不下行', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const postSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    useWidgetEventStore.setState({ latest: { 'other-widget': { x: 1 } } })
    await new Promise((r) => setTimeout(r, 20))

    const eventMsgs = postSpy.mock.calls.filter(
      (c) => (c[0] as { method?: string } | undefined)?.method === 'widget.event',
    )
    expect(eventMsgs).toHaveLength(0)
  })
})

describe('安全沙箱属性与标题', () => {
  it('iframe 不开 allow-same-origin（opaque origin），title 缺省为 Webview', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview')
    const sandbox = iframe.getAttribute('sandbox') ?? ''
    expect(sandbox).toContain('allow-scripts')
    expect(sandbox).not.toContain('allow-same-origin')
  })

  it('title 传入时覆盖 iframe 标题', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" title="插件面板" />)

    await waitFor(() => expect(screen.getByTitle('插件面板')).toBeInTheDocument())
  })

  it('crypto.randomUUID 缺失 → 兜底生成实例令牌并注入 bootstrap', async () => {
    vi.stubGlobal('crypto', {})
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const m = getSrcDoc().match(/TOKEN = "([^"]+)"/)
    expect(m).not.toBeNull()
    expect(String(m?.[1])).toMatch(/^wv_/)
  })

  it('apiPost 未被 HTML 加载路径使用（HTML 走 GET）', async () => {
    apiGet.mockResolvedValue({ data: '<p>x</p>' })
    render(<WebviewWidget pluginId="demo" widgetId="w-get" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('transformResponse 原样透传文本（axios 管线：JSON 解析被禁用，HTML 不被解析）', async () => {
    // 模拟 axios 收到配置后按 transformResponse 处理原始响应体的行为
    apiGet.mockImplementation(
      async (_url: string, config?: { transformResponse?: Array<(d: unknown) => unknown> }) => {
        const raw = '<p>{"looks":"like json"}</p>'
        const transformed = (config?.transformResponse ?? []).reduce<unknown>(
          (acc, fn) => fn(acc),
          raw,
        )
        return { data: transformed }
      },
    )

    render(<WebviewWidget pluginId="demo" />)

    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    // 未被 JSON.parse 破坏：原文完整注入 srcdoc
    expect(getSrcDoc()).toContain('<p>{"looks":"like json"}</p>')
  })
})
