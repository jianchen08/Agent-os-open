/** @feature FP-0.2.四 WebviewWidget 透明底声明（悬浮叠加形态） | @ci: frontend-test */
/**
 * WebviewWidget · 透明底声明测试（props.transparent）
 *
 * 契约：transparent=true → iframe 元素底 bg-transparent（插件页自带圆角透出
 * 宿主，悬浮叠加形态）；缺省 false → 既有 bg-[var(--web-canvas)] 画布底零变化。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { WebviewWidget } from '../WebviewWidget'

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

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body></body></html>' })
})

describe('WebviewWidget — transparent 透明底声明', () => {
  it('transparent=true：iframe 底透明（悬浮叠加形态，圆角透出宿主）', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w-t" transparent />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(screen.getByTitle('Webview').className).toContain('bg-transparent')
    expect(screen.getByTitle('Webview').className).not.toContain('web-canvas')
  })

  it('缺省 false：画布底零变化', async () => {
    render(<WebviewWidget pluginId="demo" widgetId="w-t2" />)
    await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
    expect(screen.getByTitle('Webview').className).toContain('bg-[var(--web-canvas)]')
    expect(screen.getByTitle('Webview').className).not.toContain('bg-transparent')
  })
})
