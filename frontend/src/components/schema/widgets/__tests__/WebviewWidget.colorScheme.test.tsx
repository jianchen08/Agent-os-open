/** @feature FP-0.2.四 WebviewWidget 文档色彩方案（transparent 悬浮件） | @ci: frontend-test */
/**
 * WebviewWidget · iframe 文档 color-scheme 注入测试
 *
 * 契约（2026-11-10 用户实测：深色主题下待办圆钮/面板四角出现实色方块底）：
 * - transparent=true 且生效主题深色 → srcdoc 注入 `<meta name="color-scheme"
 *   content="dark">` + bootstrap 内联 `colorScheme='dark'`：宿主深色而 iframe
 *   文档未声明 color-scheme 时 Chromium 会把文档画布涂成近白实色（透明底圆角
 *   四角随即出方块），声明成同一套明暗即恢复透明。
 * - transparent=true 且生效主题浅色 → content="light"。
 * - 缺省（画布底 bg-[var(--web-canvas)] 恒白）→ 零注入，既有观感零变化。
 * - theme.sync 下行 payload 增 scheme 字段（主题切换即时纠正，字段只增不删）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import { apiClient } from '@/services/api/client'
import { useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import { WebviewWidget } from '../WebviewWidget'
import { postUp } from './webviewTestBridge'
import type { ThemeConfig } from '@/types/theme'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/stores/sessionStore', async () => {
  const { create } = await import('zustand')
  return {
    useSessionStore: create<{ activeSessionId: string | null }>(() => ({
      activeSessionId: 'sess-a',
    })),
  }
})

const DARK = presetThemes['dark'] as ThemeConfig
const LIGHT = presetThemes['light'] as ThemeConfig

const srcdoc = (): string =>
  (screen.getByTitle('Webview') as HTMLIFrameElement).getAttribute('srcdoc') ?? ''

beforeEach(() => {
  vi.clearAllMocks()
  useSessionThemeStore.setState({ stacks: {} })
  useThemeStore.setState({ themeConfig: DARK, activePluginTheme: null })
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><head></head><body></body></html>' })
})

describe('WebviewWidget — iframe 文档 color-scheme', () => {
  it('transparent + 深色主题：注入 meta color-scheme=dark 且 bootstrap 内联同值', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w-cs-dark" transparent />)
    await waitFor(() => expect(srcdoc()).toContain('color-scheme'))
    expect(srcdoc()).toContain('<meta name="color-scheme" content="dark">')
    expect(srcdoc()).toContain('SCHEME = "dark"')
  })

  it('transparent + 浅色主题：注入 light（两组有区分度）', async () => {
    useThemeStore.setState({ themeConfig: LIGHT })
    render(<WebviewWidget pluginId="demo" widgetId="w-cs-light" transparent />)
    await waitFor(() => expect(srcdoc()).toContain('color-scheme'))
    expect(srcdoc()).toContain('<meta name="color-scheme" content="light">')
    expect(srcdoc()).toContain('SCHEME = "light"')
  })

  it('缺省（画布底形态）：零注入，保持既有观感', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w-cs-canvas" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(srcdoc()).not.toContain('color-scheme')
    expect(srcdoc()).toContain('SCHEME = null')
  })

  it('theme.sync 下行 payload 携 scheme（主题明暗随推，供 iframe 即时纠正）', async () => {
    const spy = vi.fn()
    // contentWindow 挂 prototype 层（读取时即命中，时序免疫）：iframe load 与
    // __ready 是「先到者开桥」（组件内契约），实例级 defineProperty 挂晚了会
    // 错过 load 先到时开闸的那一推——spy 必须先于任何就绪路径就位。
    const original = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow')
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
      configurable: true,
      get: () => ({ postMessage: spy }),
    })
    try {
      render(<WebviewWidget pluginId="demo" widgetId="w-cs-sync" transparent />)
      await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
      // 就绪信号 → 下行桥开闸（__ready 走 validateWebviewEvent，需带实例令牌）
      postUp('__ready', undefined, 'wv_ready', { wrapAct: true })
      await waitFor(() => {
        const msgs = spy.mock.calls
          .map((c) => c[0] as Record<string, unknown> | undefined)
          .filter((m): m is Record<string, unknown> => !!m && m.method === 'theme.sync')
        expect(msgs.length).toBeGreaterThanOrEqual(1)
        expect(msgs.at(-1)!.params).toMatchObject({ scheme: 'dark' })
      })
    } finally {
      if (original) Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', original)
    }
  })
})
