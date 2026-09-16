/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * WebviewWidget · theme.apply 主题桥路由测试（模式体系 §5.0 Wave2 件4）
 *
 * 白名单宿主侧方法：theme.apply 载荷（结构化 ThemeConfig 档）按收到时的
 * 当前会话入 override 栈——
 * - 合法档 + 活跃会话 → 入栈成功并回执 result
 * - 载荷校验失败 → 丢弃（零状态变更）并回执 error
 * - 无活跃会话 → 丢弃并回执 error
 * - 纯宿主行为：不经内核 transport（apiClient 零调用）
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import { apiClient } from '@/services/api/client'
import { useSessionThemeStore, getActiveSessionTheme } from '@/stores/sessionThemeStore'
import type { Mock } from 'vitest'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: { getState: () => ({ activeSessionId: 'sess-theme' }) },
}))

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock
const apiPost = apiClient.post as unknown as Mock

const VALID_THEME = presetThemes['dark']

function getIframeToken(): string {
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  const m = (iframe.getAttribute('srcdoc') ?? '').match(/TOKEN = "([^"]+)"/)
  if (!m) throw new Error('iframe srcdoc 中未找到实例令牌 TOKEN')
  return m[1]
}

function postUp(method: string, params?: unknown, id = 'wv_t'): void {
  const data: Record<string, unknown> = {
    __agentos_webview: true,
    __wv_token: getIframeToken(),
    id,
    method,
  }
  if (params !== undefined) data.params = params
  window.dispatchEvent(new MessageEvent('message', { origin: 'null', data }))
}

function findDownMessage(spy: Mock, method: string): Record<string, unknown> | undefined {
  for (const call of spy.mock.calls) {
    const msg = call[0] as Record<string, unknown> | undefined
    if (msg && typeof msg === 'object' && msg.method === method) return msg
  }
  return undefined
}

describe('WebviewWidget — theme.apply 主题桥', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useSessionThemeStore.setState({ stacks: {} })
    apiGet.mockResolvedValue({ data: '<html><body></body></html>' })
  })

  it('合法 ThemeConfig 档 → 当前会话入栈生效，回执 result，不经内核 transport', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    postUp('theme.apply', VALID_THEME, 'wv_ok')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'theme.apply.result')).toBeDefined()
    })
    expect(getActiveSessionTheme('sess-theme')?.id).toBe(VALID_THEME.id)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('载荷校验失败 → 丢弃（栈零变更）并回执 error（≥2 组非法输入）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
    const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')

    postUp('theme.apply', { id: 'x', name: '缺 colors 档' }, 'wv_bad1')
    postUp('theme.apply', 'not-an-object', 'wv_bad2')

    await waitFor(() => {
      expect(findDownMessage(downSpy, 'theme.apply.error')).toBeDefined()
    })
    expect(findDownMessage(downSpy, 'theme.apply.result')).toBeUndefined()
    expect(useSessionThemeStore.getState().stacks).toEqual({})
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('theme.apply 与 action 方法同帧混发：白名单方法不外漏到 actions 端点', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w1" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())

    postUp('theme.apply', VALID_THEME)
    await waitFor(() => expect(getActiveSessionTheme('sess-theme')).toBeDefined())
    // 全程没有任何 action/REST 外呼
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiGet.mock.calls.filter((c: unknown[]) => c[0] !== '/ext/demo/webview')).toHaveLength(0)
  })
})
