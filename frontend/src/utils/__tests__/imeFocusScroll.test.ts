/** @feature FP-0.2.移动端适配 键盘不遮挡任意输入框 | @ci: frontend-test */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { installImeFocusScroll } from '../imeFocusScroll'

/** 移动/桌面视口切换（isMobileViewportNow 读 window.innerWidth） */
function setViewportWidth(width: number): void {
  Object.defineProperty(window, 'innerWidth', { value: width, writable: true, configurable: true })
}

/** 等待一轮 rAF：installImeFocusScroll 的滚动统一收敛到 rAF 触发 */
function nextFrame(): Promise<void> {
  return new Promise((resolve) => requestAnimationFrame(() => resolve()))
}

function focusAndDispatchFocusIn(el: HTMLElement): void {
  el.focus()
  el.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
}

describe('installImeFocusScroll', () => {
  let cleanup: (() => void) | null = null
  let scrollIntoViewSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    scrollIntoViewSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup?.()
    cleanup = null
    vi.restoreAllMocks()
    document.body.textContent = ''
  })

  it('移动视口：文本输入框 focusin → rAF 后滚入可视区（block=nearest）', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const input = document.createElement('input')
    document.body.appendChild(input)

    focusAndDispatchFocusIn(input)
    await nextFrame()

    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewSpy).toHaveBeenCalledWith({ block: 'nearest' })
  })

  it('移动视口：键盘弹出驱动的 resize → 已聚焦输入框滚入可视区', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const input = document.createElement('textarea')
    document.body.appendChild(input)
    input.focus()

    window.dispatchEvent(new Event('resize'))
    await nextFrame()

    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
  })

  it('同一帧多次触发（focusin+连续 resize）合并为一次滚动', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()

    focusAndDispatchFocusIn(input)
    window.dispatchEvent(new Event('resize'))
    window.dispatchEvent(new Event('resize'))
    await nextFrame()

    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
  })

  it('滚动时机读 activeElement：焦点元素被替换后滚的是新焦点', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const first = document.createElement('input')
    const second = document.createElement('input')
    document.body.append(first, second)

    focusAndDispatchFocusIn(first)
    // rAF 到来前焦点已移走（如弹层 autofocus 抢焦点）：滚当前聚焦者而非先聚焦者
    second.focus()
    await nextFrame()

    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(second)
  })

  it('桌面视口：focusin 与 resize 均不动作', async () => {
    setViewportWidth(1280)
    cleanup = installImeFocusScroll()
    const input = document.createElement('input')
    document.body.appendChild(input)

    focusAndDispatchFocusIn(input)
    window.dispatchEvent(new Event('resize'))
    await nextFrame()

    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it.each<[string, string, (el: HTMLElement) => void]>([
    ['checkbox input', 'input', (el) => ((el as HTMLInputElement).type = 'checkbox')],
    ['readonly input', 'input', (el) => ((el as HTMLInputElement).readOnly = true)],
    ['disabled select', 'select', (el) => ((el as HTMLSelectElement).disabled = true)],
  ])('不唤起键盘的元素 %s focusin → 不滚动', async (_label, tagName, mutate) => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const el = document.createElement(tagName)
    document.body.appendChild(el)
    mutate(el)

    focusAndDispatchFocusIn(el)
    await nextFrame()

    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('非可编辑元素 focusin → 不滚动', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const div = document.createElement('div')
    document.body.appendChild(div)

    div.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    await nextFrame()

    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('contenteditable 元素按可编辑处理', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()
    const div = document.createElement('div')
    document.body.appendChild(div)
    Object.defineProperty(div, 'isContentEditable', { value: true })
    // jsdom 非焦点元素 dispatch focusin 不改变 activeElement，stub 聚焦态
    const activeElementSpy = vi
      .spyOn(document, 'activeElement', 'get')
      .mockReturnValue(div)

    div.dispatchEvent(new FocusEvent('focusin', { bubbles: true }))
    await nextFrame()

    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
    expect(scrollIntoViewSpy.mock.contexts[0]).toBe(div)
    activeElementSpy.mockRestore()
  })

  it('键盘收起时无聚焦元素（activeElement=body）→ 不滚动', async () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()

    window.dispatchEvent(new Event('resize'))
    await nextFrame()

    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })

  it('卸载后 resize 不再触发滚动', async () => {
    setViewportWidth(390)
    const dispose = installImeFocusScroll()
    const input = document.createElement('input')
    document.body.appendChild(input)
    input.focus()
    dispose()

    window.dispatchEvent(new Event('resize'))
    await nextFrame()

    expect(scrollIntoViewSpy).not.toHaveBeenCalled()
  })
})
