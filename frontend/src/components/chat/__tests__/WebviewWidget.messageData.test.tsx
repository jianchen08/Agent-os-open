// @feature: FP-T12 前端适配(webview卡数据) | @ci: frontend-test
// @feature: message-segments P0 | @ci: frontend-test
/**
 * 消息卡宿主桥（宿主 → 卡）：WebviewWidget message.data 下行信封。
 *
 * 契约（web/cards/compression.html 文件头输入契约）：卡监听
 * {__agentos_webview:true, method:"message.data", params:{message:{content,metadata}}}；
 * 宿主在 webview 就绪（iframe load / 上行 __ready）后推送一次。未提供
 * injectMessage（通用 widget）时零推送——theme.sync/ctx.sync 桥不受影响。
 */
import { fireEvent, render, waitFor } from '@testing-library/react'
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

const { apiClientGet } = vi.hoisted(() => ({ apiClientGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({
  apiClient: { get: apiClientGet, post: vi.fn() },
}))

import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'

const postMessageMock = vi.fn()

beforeAll(() => {
  // jsdom 的 srcDoc 加载是异步的：原生 load 事件可能先于用例的 stub 到达，
  // 在原型层替换 contentWindow 使任意时序的就绪推送都落到可观测的 mock 上
  Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    configurable: true,
    get() {
      return { postMessage: postMessageMock }
    },
  })
})

/** 渲染并等就绪（load 事件 = webviewReady 门闩），返回下行消息列表 */
async function renderReadyAndCollect(injectMessage?: {
  content: string
  metadata?: Record<string, unknown> | null
}): Promise<Array<{ method?: string; params?: unknown; __agentos_webview?: boolean }>> {
  const { container } = render(
    <WebviewWidget pluginId="context_window_guard" htmlPath="/webview/compression_card.html" injectMessage={injectMessage} />,
  )
  await waitFor(() => {
    expect(container.querySelector('iframe')).not.toBeNull()
  })
  fireEvent.load(container.querySelector('iframe')!)
  await waitFor(() => {
    expect(postMessageMock.mock.calls.length).toBeGreaterThan(0)
  })
  return postMessageMock.mock.calls.map(
    ([m]) => m as { method?: string; params?: unknown; __agentos_webview?: boolean },
  )
}

describe('WebviewWidget — message.data 下行信封', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    postMessageMock.mockClear()
    apiClientGet.mockResolvedValue({
      data: '<html><head><title>t</title></head><body>card</body></html>',
    })
  })

  it('就绪后按卡输入契约推送 message.data（params.message = content + metadata）', async () => {
    const messages = await renderReadyAndCollect({
      content: '<compressed seq="40-55">…</compressed>',
      metadata: { compression_ref: { segment_id: 'seg-1', seq_range: [40, 55] } },
    })
    const dataMsg = messages.find((m) => m.method === 'message.data')
    expect(dataMsg).toBeDefined()
    expect(dataMsg?.__agentos_webview).toBe(true)
    expect(dataMsg?.params).toEqual({
      message: {
        content: '<compressed seq="40-55">…</compressed>',
        metadata: { compression_ref: { segment_id: 'seg-1', seq_range: [40, 55] } },
      },
    })
  })

  it('未提供 injectMessage → 不推 message.data，theme.sync/ctx.sync 桥不受影响', async () => {
    const messages = await renderReadyAndCollect()
    expect(messages.some((m) => m.method === 'message.data')).toBe(false)
    expect(messages.some((m) => m.method === 'theme.sync')).toBe(true)
    expect(messages.some((m) => m.method === 'ctx.sync')).toBe(true)
  })
})
