/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * WebviewWidget 桥缺口补测（themeApply/hostBridge 主链已有专测，这里补两处
 * 边缘分支）：
 * - theme.apply 在宿主无活跃会话时 fail-closed：整包丢弃并回 error（零状态变更）
 * - message.data 下行桥（消息卡宿主桥）：就绪后按卡输入契约推送宿主消息数据
 *
 * 外部依赖 mock：apiClient（HTML 取数）、sessionStore（真 zustand store，
 * setState 驱动会话态）；其余走真实实现。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { useSessionStore } from '@/stores/sessionStore'
import { allDownByMethod, findDownMessage, postUp as bridgePostUp } from './webviewTestBridge'
import type { Mock } from 'vitest'

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

import { WebviewWidget } from '../WebviewWidget'

const apiGet = apiClient.get as unknown as Mock

const THEME_PROFILE = {
  colors: { bg: '#101014', fg: '#f5f5f5' },
}

beforeEach(() => {
  vi.clearAllMocks()
  useSessionStore.setState({ activeSessionId: 'sess-a' })
  apiGet.mockResolvedValue({ data: '<html><body></body></html>' })
})

async function renderWidget(props: Record<string, unknown> = {}): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w1" {...props} />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

// 全量套件下 iframe src 重载会替换 contentWindow：早先装的 spy 落在旧窗口上
// 永远等不到下行调用。断言前按当前窗口重取 spy（vi.spyOn 幂等，同窗口同 spy）。
function currentDownSpy(): Mock {
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  return vi.spyOn(iframe.contentWindow!, 'postMessage')
}

describe('WebviewWidget · 桥边缘分支', () => {
  it('theme.apply 无活跃会话：整包丢弃回 error，不写会话主题栈', async () => {
    useSessionStore.setState({ activeSessionId: null })
    const downSpy = await renderWidget()

    act(() => {
      bridgePostUp('theme.apply', THEME_PROFILE, 'wv_nosess')
    })

    const err = findDownMessage(downSpy, 'theme.apply.error')
    expect(err).toBeDefined()
    expect(String(err!.params?.message)).toContain('无活跃会话')
    expect(findDownMessage(downSpy, 'theme.apply.result')).toBeUndefined()
    expect(apiGet.mock.calls.filter((c: unknown[]) => c[0] !== '/ext/demo/webview')).toHaveLength(0)
  })

  it('message.data 下行桥：就绪后按卡输入契约推送宿主消息（content + metadata）', async () => {
    const injectMessage = {
      content: '（压缩摘要）前情：勇者进入洞窟……',
      metadata: { compression_ref: 'cmp_42' },
    }
    const downSpy = await renderWidget({ injectMessage })

    act(() => {
      bridgePostUp('__ready', {}, 'wv_ready')
    })

    await waitFor(
      () => {
        expect(allDownByMethod(currentDownSpy(), 'message.data')).toHaveLength(1)
      },
      // 全量套件高负载下就绪链路延迟可达数秒（CI 实证 3s+），放宽轮询上限
      { timeout: 8000 },
    )
    const down = allDownByMethod(currentDownSpy(), 'message.data')[0]!
    expect(down.params).toEqual({ message: injectMessage })
  })

  it('未带 injectMessage 的通用 widget：就绪后不推 message.data（不伪造卡数据）', async () => {
    const downSpy = await renderWidget()
    act(() => {
      bridgePostUp('__ready', {}, 'wv_ready2')
    })
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(allDownByMethod(downSpy, 'message.data')).toHaveLength(0)
  })
})
