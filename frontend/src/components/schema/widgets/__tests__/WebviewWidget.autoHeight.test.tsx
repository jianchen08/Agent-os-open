/** @feature FP-RP面板v3.1刀3 | @ci: frontend-test */
/**
 * WebviewWidget · 自适应尺寸通用上行桥测试（webview.resize）
 * （ADR 2026-10-14-message-card-body-form）
 *
 * 契约：autoHeight 开启时上行 webview.resize {height} 撑开容器（clamp
 * [24, 4000]px + result 回执），可选 width 同桥回报容器宽（clamp [0, 4000]，
 * 缺省不动宽——既有纯高度卡零变化）；非数字 height/width / 未开启实例 →
 * error 回执 fail-closed；缺省关闭 = 既有 h-full 填充形态零变化。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { WEBVIEW_MAX_HEIGHT, WEBVIEW_MIN_HEIGHT, WebviewWidget } from '../WebviewWidget'
import { allDownByMethod, postUp as bridgePostUp } from './webviewTestBridge'
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

function postUp(method: string, params?: unknown, id = 'wv_ah'): void {
  bridgePostUp(method, params, id, { wrapAct: true })
}

async function renderReady(autoHeight: boolean): Promise<Mock> {
  render(<WebviewWidget pluginId="demo" widgetId="w-ah" autoHeight={autoHeight} />)
  await waitFor(() => expect(screen.getByTitle('Webview')).toBeInTheDocument())
  const iframe = screen.getByTitle('Webview') as HTMLIFrameElement
  const downSpy = vi.spyOn(iframe.contentWindow!, 'postMessage')
  postUp('__ready')
  return downSpy
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body></body></html>' })
})

describe('WebviewWidget — webview.resize 自适应高度桥', () => {
  it('autoHeight 开启：合法高度撑开容器（style.height 落值 + result 回执）', async () => {
    const downSpy = await renderReady(true)
    postUp('webview.resize', { height: 120 })

    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(1))
    expect(allDownByMethod(downSpy, 'webview.resize.result')[0]!.params).toEqual({
      applied: true,
      height: 120,
    })
    const wrapper = screen.getByTitle('Webview').parentElement!
    expect(wrapper.style.height).toBe('120px')
  })

  it('clamp 界内夹取：超上界压到 4000、低于下界抬到 24（性质断言：出界值必夹回界内）', async () => {
    const downSpy = await renderReady(true)
    postUp('webview.resize', { height: 999_999 }, 'wv_hi')
    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(1))
    expect(
      (allDownByMethod(downSpy, 'webview.resize.result').at(-1)!.params as { height: number }).height,
    ).toBe(WEBVIEW_MAX_HEIGHT)

    postUp('webview.resize', { height: -50 }, 'wv_lo')
    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(2))
    expect(
      (allDownByMethod(downSpy, 'webview.resize.result').at(-1)!.params as { height: number }).height,
    ).toBe(WEBVIEW_MIN_HEIGHT)
  })

  it('非数字 height → error 回执零状态变更（fail-closed）', async () => {
    const downSpy = await renderReady(true)
    const before = screen.getByTitle('Webview').parentElement!.style.height
    postUp('webview.resize', { height: 'tall' })

    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.error')).toHaveLength(1))
    expect(screen.getByTitle('Webview').parentElement!.style.height).toBe(before)
  })

  it('未开启实例（缺省）：webview.resize 回 error，容器保持 h-full 填充形态', async () => {
    const downSpy = await renderReady(false)
    postUp('webview.resize', { height: 120 })

    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.error')).toHaveLength(1))
    const wrapper = screen.getByTitle('Webview').parentElement!
    expect(wrapper.style.height).toBe('')
    expect(wrapper.className).toContain('h-full')
  })
})

describe('WebviewWidget — webview.resize 可选宽度桥（悬浮槽 shrink-to-fit 实宽自报）', () => {
  it('height+width 同报：容器宽高都落值，result 回执带 width', async () => {
    const downSpy = await renderReady(true)
    postUp('webview.resize', { height: 28, width: 52 })

    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(1))
    expect(allDownByMethod(downSpy, 'webview.resize.result')[0]!.params).toEqual({
      applied: true,
      height: 28,
      width: 52,
    })
    const wrapper = screen.getByTitle('Webview').parentElement!
    expect(wrapper.style.height).toBe('28px')
    expect(wrapper.style.width).toBe('52px')
  })

  it('width 缺省：容器宽不动（style.width 空 = 既有纯高度卡零变化）', async () => {
    const downSpy = await renderReady(true)
    postUp('webview.resize', { height: 120 })

    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(1))
    expect(screen.getByTitle('Webview').parentElement!.style.width).toBe('')
  })

  it('width 负值夹 0（隐藏态合法）、非数字 width → error 零状态变更（fail-closed）', async () => {
    const downSpy = await renderReady(true)
    postUp('webview.resize', { height: 24, width: -10 }, 'wv_neg')
    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.result')).toHaveLength(1))
    expect(
      (allDownByMethod(downSpy, 'webview.resize.result').at(-1)!.params as { width: number }).width,
    ).toBe(0)

    const widthBefore = screen.getByTitle('Webview').parentElement!.style.width
    postUp('webview.resize', { height: 24, width: 'wide' }, 'wv_bad')
    await waitFor(() => expect(allDownByMethod(downSpy, 'webview.resize.error')).toHaveLength(1))
    expect(screen.getByTitle('Webview').parentElement!.style.width).toBe(widthBefore)
  })
})
