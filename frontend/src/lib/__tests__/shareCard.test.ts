/** @feature FP-0.2.〇 核心批·分享卡 | @vision V6 可即用
 * 分享卡工具单测：锚点定位 + 降级策略（剪贴板失败→下载）。
 * html-to-image 渲染依赖真实 DOM 渲染管线，captureMessageCard 的 toPng 以桩替
 * 验证透传（捕获→PNG dataURL 的透传契约在 jsdom 可断言；浏览器级渲染证据归
 * 功能验证环节 Playwright 车道）。剪贴板写入链（fetch blob → ClipboardItem →
 * clipboard.write）与环境不支持时的抛错降级均有覆盖。
 */
// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { captureMessageCard, copyPngToClipboard, downloadDataUrl, findMessageCard } from '../shareCard'

const toPngMock = vi.fn<() => Promise<string>>()
vi.mock('html-to-image', () => ({
  toPng: (...args: unknown[]) => toPngMock(...(args as [HTMLElement, unknown])),
}))

describe('findMessageCard（分享卡锚点定位）', () => {
  it('从操作按钮向上定位到 message-item 气泡根', () => {
    document.body.innerHTML = `
      <div data-testid="message-item"><div id="bubble"><button id="btn">share</button></div></div>
    `
    const btn = document.getElementById('btn')
    expect(findMessageCard(btn)).not.toBeNull()
    expect(findMessageCard(btn)!.dataset.testid).toBe('message-item')
  })

  it('锚点外元素返回 null（不在气泡内）', () => {
    document.body.innerHTML = `<div id="orphan"><button id="btn2">x</button></div>`
    const btn = document.getElementById('btn2')
    expect(findMessageCard(btn)).toBeNull()
  })

  it('传入 null 不抛错', () => {
    expect(findMessageCard(null)).toBeNull()
  })
})

describe('分享卡降级链（剪贴板→下载）', () => {
  beforeEach(() => {
    // 清理 download 产生的临时 <a>
    document.body.innerHTML = ''
  })

  it('downloadDataUrl 触发一次带 filename 的下载点击', () => {
    const clickSpy = vi.fn()
    const origCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      const el = origCreate(tag)
      if (tag === 'a') {
        el.click = clickSpy
      }
      return el
    })
    downloadDataUrl('data:image/png;base64,AAA', 'card.png')
    expect(clickSpy).toHaveBeenCalledTimes(1)
    vi.restoreAllMocks()
  })
})

describe('captureMessageCard（DOM→PNG 透传）', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('以固定捕获选项调用 toPng 并透传 dataURL', async () => {
    toPngMock.mockResolvedValue('data:image/png;base64,CCC')
    const card = document.createElement('div')
    await expect(captureMessageCard(card)).resolves.toBe('data:image/png;base64,CCC')
    expect(toPngMock).toHaveBeenCalledTimes(1)
    expect(toPngMock.mock.calls[0][0]).toBe(card)
    expect(toPngMock.mock.calls[0][1]).toMatchObject({ cacheBust: true, pixelRatio: 2 })
  })

  it('toPng 失败时原样抛出（调用方 toast 降级，不静默）', async () => {
    toPngMock.mockRejectedValue(new Error('capture failed'))
    await expect(captureMessageCard(document.createElement('div'))).rejects.toThrow(
      'capture failed',
    )
  })
})

describe('copyPngToClipboard（PNG 写剪贴板）', () => {
  const PNG_BLOB = new Blob(['png-bytes'], { type: 'image/png' })

  function stubFetch() {
    vi.stubGlobal('fetch', vi.fn(async () => ({ blob: async () => PNG_BLOB })))
  }

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('环境支持时：fetch blob → ClipboardItem → clipboard.write 一次', async () => {
    stubFetch()
    const written: unknown[] = []
    const writeMock = vi.fn(async (items: unknown[]) => {
      written.push(...items)
    })
    Object.defineProperty(navigator, 'clipboard', {
      value: { write: writeMock },
      configurable: true,
    })
    const seenBlobs: Blob[] = []
    class FakeClipboardItem {
      constructor(public data: Record<string, Blob>) {
        seenBlobs.push(...Object.values(data))
      }
    }
    vi.stubGlobal('ClipboardItem', FakeClipboardItem)

    await expect(copyPngToClipboard('data:image/png;base64,AAA')).resolves.toBeUndefined()
    expect(writeMock).toHaveBeenCalledTimes(1)
    expect(seenBlobs[0]).toBe(PNG_BLOB)
    expect(written[0]).toBeInstanceOf(FakeClipboardItem)
  })

  it('不支持 ClipboardItem 时抛错（交由调用方降级下载）', async () => {
    stubFetch()
    vi.stubGlobal('ClipboardItem', undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: { write: vi.fn() },
      configurable: true,
    })
    await expect(copyPngToClipboard('data:image/png;base64,AAA')).rejects.toThrow(
      'clipboard image unsupported',
    )
  })

  it('clipboard.write 缺失时同样抛错', async () => {
    stubFetch()
    vi.stubGlobal('ClipboardItem', class FakeItem {})
    Object.defineProperty(navigator, 'clipboard', {
      value: {},
      configurable: true,
    })
    await expect(copyPngToClipboard('data:image/png;base64,AAA')).rejects.toThrow(
      'clipboard image unsupported',
    )
  })
})
