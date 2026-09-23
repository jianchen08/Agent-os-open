/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 标签渐变遮蔽收边契约（useFadeOverflow + TabLabel）
 *
 * 用户裁定（2026-09-21）：标签默认固定宽度上限，超长标题**用渐变遮蔽收边**
 * 淡出尾部（不是省略号）；被挤压时标签再缩小、少显示一部分文字。
 *
 * 断行为不断实现：不复制渐变色值，只断言可观察契约——
 * - 未溢出：无遮蔽样式（元素是普通文字形态）
 * - 溢出：挂上 mask 渐变，且渐变宽度随可见宽度收敛（窄标签遮蔽更短）
 * - 宽度变化（ResizeObserver）驱动遮蔽出现/消失
 * - 完整标题始终保留在文本节点与 title 上（遮蔽只影响呈现，不丢数据）
 *
 * jsdom 无布局引擎，用可控几何打桩：劫持 scrollWidth/clientWidth 的 getter 模拟
 * 「文字比容器宽」与「宽度后来变化」，并用真实 ResizeObserver 替身触发回调。
 */

import { act, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { TabLabel } from '@/components/layout/TabLabel'
import { fadeGradientFor, fadeWidthFor } from '@/hooks/useFadeOverflow'

/** 可控几何 + 可触发的 ResizeObserver 替身 */
class ResizeObserverHarness implements Partial<ResizeObserver> {
  static instances: ResizeObserverHarness[] = []
  callback: ResizeObserverCallback
  targets: Element[] = []
  constructor(cb: ResizeObserverCallback) {
    this.callback = cb
    ResizeObserverHarness.instances.push(this)
  }
  observe(el: Element): void {
    this.targets.push(el)
  }
  unobserve(): void {}
  disconnect(): void {
    this.targets = []
  }
  /** 模拟元素尺寸变化 */
  fire(): void {
    this.callback([], this as unknown as ResizeObserver)
  }
}

/** 把元素的 scrollWidth/clientWidth 钉到指定值（jsdom 无布局） */
function stubGeometry(el: HTMLElement, contentW: number, visibleW: number): void {
  Object.defineProperty(el, 'scrollWidth', { value: contentW, configurable: true })
  Object.defineProperty(el, 'clientWidth', { value: visibleW, configurable: true })
}

const LONG = '一个非常非常长的标签标题需要在尾部渐隐而不是硬切'

/** 渲染溢出标签（文字 320 / 容器 120）并触发首次复量，返回元素与观察器 */
function renderOverflownLabel(): { el: HTMLElement; ro: ResizeObserverHarness | undefined } {
  render(<TabLabel title={LONG} />)
  const el = screen.getByTestId('tab-label')
  stubGeometry(el, 320, 120)
  const ro = ResizeObserverHarness.instances[0]
  act(() => ro?.fire())
  return { el, ro }
}

describe('遮蔽算式 —— 宽度随可见宽度收敛并可安全重复', () => {
  it('窄标签遮蔽更短，宽标签封顶（保证文字主体清晰）', () => {
    expect(fadeWidthFor(64)).toBeLessThan(fadeWidthFor(400))
    expect(fadeWidthFor(400)).toBeLessThanOrEqual(20)
    expect(fadeWidthFor(64)).toBeGreaterThanOrEqual(8)
  })

  it('同一宽度的结果稳定（幂等，可安全重复渲染）', () => {
    expect(fadeWidthFor(120)).toBe(fadeWidthFor(120))
    expect(fadeGradientFor(120)).toBe(fadeGradientFor(120))
  })

  it('生成的渐变不含 calc（运行时与 jsdom 口径一致），且以 transparent 收尾', () => {
    const gradient = fadeGradientFor(120)
    expect(gradient).toMatch(/^linear-gradient\(to right, black \d+%, transparent 100%\)$/)
    expect(gradient).not.toContain('calc')
  })

  it('可见宽度为 0（未挂载/无布局）时回退到目标比例，不产生 NaN', () => {
    expect(fadeGradientFor(0)).not.toContain('NaN')
    expect(fadeGradientFor(0)).toMatch(/black \d+%/)
  })
})

describe('TabLabel —— 渐变遮蔽收边', () => {
  beforeEach(() => {
    ResizeObserverHarness.instances = []
    globalThis.ResizeObserver = ResizeObserverHarness as unknown as typeof ResizeObserver
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('未溢出时无遮蔽样式（普通文字形态）', () => {
    render(<TabLabel title="短" />)
    const el = screen.getByTestId('tab-label')
    stubGeometry(el, 30, 120)
    // 触发一次复量
    const ro = ResizeObserverHarness.instances[0]
    act(() => ro?.fire())
    expect(el.getAttribute('data-overflowing')).toBe('false')
    expect(el.style.getPropertyValue('mask-image')).toBe('')
  })

  it('溢出时挂上 mask 渐变并标记溢出（尾部淡出取代省略号）', () => {
    const { el } = renderOverflownLabel()
    expect(el.getAttribute('data-overflowing')).toBe('true')
    expect(el.style.getPropertyValue('mask-image')).toMatch(/linear-gradient/)
    expect(el.style.getPropertyValue('mask-image')).toContain('transparent')
    // webkit 前缀同步（Chromium 走前缀属性）
    expect(el.style.getPropertyValue('-webkit-mask-image')).toMatch(/linear-gradient/)
  })

  it('宽度从窄变宽使溢出消失时，遮蔽随之撤掉（不残留）', () => {
    const { el, ro } = renderOverflownLabel()
    expect(el.getAttribute('data-overflowing')).toBe('true')

    stubGeometry(el, 100, 300) // 容器变宽，文字不再溢出
    act(() => ro?.fire())
    expect(el.getAttribute('data-overflowing')).toBe('false')
    expect(el.style.getPropertyValue('mask-image')).toBe('')
  })

  it('遮蔽宽度随可见宽度收敛：窄标签的渐变宽度小于宽标签', () => {
    render(<TabLabel title={LONG} />)
    const el = screen.getByTestId('tab-label')
    const ro = ResizeObserverHarness.instances[0]
    // 遮蔽宽度以百分比停点表达：停点越小 → 遮蔽越宽
    const fadeStop = () =>
      Number((el.style.getPropertyValue('mask-image').match(/black (\d+)%/) || [])[1] ?? Number.NaN)

    stubGeometry(el, 900, 64)
    act(() => ro?.fire())
    const narrowStop = fadeStop()

    stubGeometry(el, 900, 400)
    act(() => ro?.fire())
    const wideStop = fadeStop()

    expect(Number.isNaN(narrowStop)).toBe(false)
    // 窄标签的渐变停点更靠左 → 遮蔽更宽（提示「尾部还有内容」）
    expect(narrowStop).toBeLessThan(wideStop)
  })

  it('完整标题始终保留在文本节点与 title 上（遮蔽只影响呈现，不丢数据）', () => {
    render(<TabLabel title={LONG} />)
    const el = screen.getByTestId('tab-label')
    stubGeometry(el, 900, 80)
    act(() => ResizeObserverHarness.instances[0]?.fire())
    expect(el.textContent).toBe(LONG)
    expect(el.className).toContain('min-w-0')
    expect(el.className).toContain('overflow-hidden')
  })

  it('不订阅已卸载元素：卸载后触发回调不报错', () => {
    const { unmount } = render(<TabLabel title={LONG} />)
    const ro = ResizeObserverHarness.instances[0]
    const el = screen.getByTestId('tab-label')
    stubGeometry(el, 900, 80)
    unmount()
    expect(() => act(() => ro?.fire())).not.toThrow()
  })
})
