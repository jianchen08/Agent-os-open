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


/**
 * visualViewport 键盘缺口补偿（本 describe 新增）
 *
 * 背景契约：布局视口（window.innerHeight）与视觉视口（visualViewport）的
 * 差值 ≥ 阈值 = 软键盘占位（iOS Safari 新键盘模型不缩布局视口，Android
 * Chrome interactive-widget=resizes-content 已缩 innerHeight——两种模型
 * 下 gap 都在键盘弹出时增大）。补偿器把 gap 写入根节点 CSS 变量
 * --ime-keyboard-gap，钉底输入条用它上移到键盘上沿；原生垫高健康的
 * 环境（壳 ime padding 生效）innerHeight 已被缩掉键盘高度，gap≈0，
 * 变量恒 0px，零叠加。
 */
describe('visualViewport 键盘缺口补偿', () => {
  const GAP_VAR = '--ime-keyboard-gap'

  /** 可控 fake visualViewport：gap=键盘占位高度，fire 模拟视口 resize */
  function installFakeVV(initialGap: number) {
    const listeners = new Set<() => void>()
    let gap = initialGap
    let topOffset = 0
    const vv = {
      get offsetTop() {
        return topOffset
      },
      get offsetLeft() {
        return 0
      },
      get height() {
        return window.innerHeight - gap
      },
      addEventListener: (type: string, fn: () => void) => {
        if (type === 'resize') listeners.add(fn)
      },
      removeEventListener: (type: string, fn: () => void) => {
        if (type === 'resize') listeners.delete(fn)
      },
    } as unknown as VisualViewport
    Object.defineProperty(window, 'visualViewport', { value: vv, configurable: true })
    return {
      fire(nextGap: number) {
        gap = nextGap
        listeners.forEach((fn) => fn())
      },
      /** 视觉视口顶部偏移（iOS Safari 页面被键盘上推时 >0） */
      setTopOffset(v: number) {
        topOffset = v
      },
      listenerCount() {
        return listeners.size
      },
      remove() {
        Reflect.deleteProperty(window, 'visualViewport')
      },
    }
  }

  let cleanup: (() => void) | null = null
  let fakeVV: ReturnType<typeof installFakeVV> | null = null
  let scrollIntoViewSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    scrollIntoViewSpy = vi.spyOn(Element.prototype, 'scrollIntoView').mockImplementation(() => {})
  })

  afterEach(() => {
    cleanup?.()
    cleanup = null
    fakeVV?.remove()
    fakeVV = null
    document.documentElement.style.removeProperty(GAP_VAR)
    vi.restoreAllMocks()
    document.body.textContent = ''
  })

  it('键盘弹出：gap 写入根节点变量，且随键盘高度变化跟踪（性质断言：var ≡ gap）', () => {
    setViewportWidth(390)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()

    fakeVV.fire(300)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('300px')

    // 第二组输入：键盘高度变化（换输入法/悬浮键盘）→ 变量同步更新
    fakeVV.fire(260)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('260px')
  })

  it('键盘收起：gap 归零 → 变量复位 0px', () => {
    setViewportWidth(390)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()

    fakeVV.fire(300)
    fakeVV.fire(0)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('0px')
  })

  it('gap 低于键盘判定阈值（iOS Safari 地址栏伸缩差 ~60px）→ 不视为键盘，变量恒 0px', () => {
    setViewportWidth(390)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()

    fakeVV.fire(60)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('0px')

    // 第二组输入：更小的 chrome UI 差值同样归零
    fakeVV.fire(20)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('0px')
  })

  it('桌面视口：visualViewport 变化不写变量（门控同源 768 断点）', () => {
    setViewportWidth(1280)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()

    fakeVV.fire(300)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('')
  })

  it('无 visualViewport 环境（老浏览器/纯净 jsdom）→ 不写变量且不抛错', () => {
    setViewportWidth(390)
    cleanup = installImeFocusScroll()

    window.dispatchEvent(new Event('resize'))
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('')
  })

  it('键盘弹出 + 已聚焦输入框 → 输入框滚入可视区（scrollIntoView 一次）', () => {
    setViewportWidth(390)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()
    const input = document.createElement('textarea')
    document.body.appendChild(input)
    input.focus()

    fakeVV.fire(300)
    // vv resize 的滚动与既有窗口 resize 一样收敛到 rAF
    return new Promise<void>((resolve) =>
      requestAnimationFrame(() => {
        expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1)
        expect(scrollIntoViewSpy.mock.contexts[0]).toBe(input)
        resolve()
      }),
    )
  })

  it('键盘高度公式：var = innerHeight − vv.height − vv.offsetTop（iOS Safari 键盘上推页面时 offsetTop>0）', () => {
    setViewportWidth(390)
    // 第一组：无上推，offsetTop=0，键盘 300 → var=300
    fakeVV = installFakeVV(300)
    cleanup = installImeFocusScroll()
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('300px')

    // 第二组：同键盘但页面上推 50px（visual viewport 下缘=键盘上沿在布局坐标中上移）
    fakeVV.setTopOffset(50)
    fakeVV.fire(300)
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('250px')
  })

  it('卸载后 visualViewport resize 不再写变量', () => {
    setViewportWidth(390)
    fakeVV = installFakeVV(0)
    cleanup = installImeFocusScroll()
    // 安装时初始写入（键盘已在屏的冷启动场景）：gap=0 → '0px'
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('0px')
    expect(fakeVV.listenerCount()).toBe(1)
    cleanup()
    cleanup = null

    fakeVV.fire(300)
    // 卸载后不再写入：变量停在卸载前的值，不随键盘事件变化
    expect(document.documentElement.style.getPropertyValue(GAP_VAR)).toBe('0px')
    expect(fakeVV.listenerCount()).toBe(0)
  })
})
