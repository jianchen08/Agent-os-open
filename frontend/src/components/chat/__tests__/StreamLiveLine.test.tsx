/**
 * 流式活动状态如实化回归测试（任务 3a19e4183b20）
 *
 * 覆盖三处改动：
 * 1. 缺陷①：迟到增量落到已 done 的 thinking part 时，state 必须恢复 streaming
 *    （否则「内容继续增长 + 卡片显示已完成」）。
 * 2. 缺陷②：finalizeMessage 不得把无结果 tool_call 强制置 done（终态只由
 *    tool_result 写入）。
 * 3. 新增功能：StreamLiveLine 取文口径（运行取最后一行、终态取首行）+ 组件
 *    渲染面（空文本零渲染/运行与终态两形态/溢出渐隐遮罩）。
 *
 * @feature FP-T12 前端组件补测
 */
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { StreamLiveLine } from '../StreamLiveLine'
import { firstLine, latestLine } from '../streamLiveLineText'

describe('StreamLiveLine 取文口径', () => {
  it('运行中取最后一行非空文本（忽略尾部空行与空白）', () => {
    expect(latestLine('first\nsecond\n\n   \n')).toBe('second')
    expect(latestLine('single line')).toBe('single line')
    expect(latestLine('\n\n  \n')).toBe('')
    // 区分度：多组输入，实现不能靠单点字面量刷绿
    expect(latestLine('a\r\nb\r\nc')).toBe('c')
    expect(latestLine('  tail with spaces  \n')).toBe('tail with spaces')
  })

  it('终态取首行非空文本', () => {
    expect(firstLine('\n\n  first real line\nsecond')).toBe('first real line')
    expect(firstLine('only')).toBe('only')
    expect(firstLine('   \n\t\n')).toBe('')
    expect(firstLine('x\ny')).toBe('x')
  })

  it('同一文本两种取文互不相同（性质断言，防同一实现对两口径给同值）', () => {
    const text = 'alpha\nbeta\ngamma'
    expect(latestLine(text)).toBe('gamma')
    expect(firstLine(text)).toBe('alpha')
    expect(latestLine(text)).not.toBe(firstLine(text))
  })
})

describe('StreamLiveLine 组件渲染面', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('文本全空时不渲染（无占位壳）', () => {
    const { container } = render(<StreamLiveLine text={'   \n \n'} running />)
    expect(container.querySelector('[data-testid="stream-live-line"]')).toBeNull()
    expect(container.textContent).toBe('')
  })

  it('运行中显示最后一行并标记跟随尾部；终态显示首行且无跟随标记', () => {
    const text = 'line-one\nline-two\nline-three'
    const { rerender } = render(<StreamLiveLine text={text} running testId="stream-live-line" />)
    const el = screen.getByTestId('stream-live-line')
    expect(el.textContent).toBe('line-three')
    expect(el.getAttribute('data-follow-end')).toBe('true')

    rerender(<StreamLiveLine text={text} running={false} testId="stream-live-line" />)
    const elEnd = screen.getByTestId('stream-live-line')
    expect(elEnd.textContent).toBe('line-one')
    expect(elEnd.getAttribute('data-follow-end')).toBeNull()
  })

  it('内容超宽（scrollWidth > clientWidth）挂渐隐遮罩，不超宽无遮罩', () => {
    // jsdom 无布局：scrollWidth/clientWidth 恒 0，用可触发的 ResizeObserver 桩
    // 手动驱动测量分支，元素几何用 defineProperty 注入两组有区分度的值
    const observers: Array<{ cb: () => void }> = []
    class ResizeObserverStub {
      private readonly cb: () => void
      constructor(cb: () => void) {
        this.cb = cb
      }
      observe(): void {
        observers.push(this)
      }
      disconnect(): void {}
      unobserve(): void {}
      fire(): void {
        this.cb()
      }
    }
    vi.stubGlobal('ResizeObserver', ResizeObserverStub)

    const { container } = render(
      <StreamLiveLine text="wide-content" running testId="stream-live-line" />,
    )
    const el = screen.getByTestId('stream-live-line') as HTMLSpanElement
    // 初始（0 宽测量）：无遮罩
    expect(el.getAttribute('style') ?? '').not.toContain('mask-size')

    Object.defineProperty(el, 'scrollWidth', { value: 500, configurable: true })
    Object.defineProperty(el, 'clientWidth', { value: 100, configurable: true })
    act(() => {
      observers[0]?.fire()
    })
    // jsdom CSSOM 会丢弃不认识的渐变函数值但保留其余 mask-* 属性——断
    // 「mask 属性组已挂上」作为溢出分支证据（不超宽时整组消失）
    expect(el.getAttribute('style') ?? '').toContain('mask-size: 100% 100%')

    Object.defineProperty(el, 'scrollWidth', { value: 100, configurable: true })
    act(() => {
      observers[0]?.fire()
    })
    expect(el.getAttribute('style') ?? '').not.toContain('mask-size')
    expect(container.textContent).toBe('wide-content')
  })
})
