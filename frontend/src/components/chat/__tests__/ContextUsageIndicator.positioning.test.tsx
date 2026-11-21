/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 上下文用量浮窗定位约束：浮窗只允许落在消息流区内（按流区大小与圆环位置夹紧）。
 *
 * 断行为：
 * - 聊天场景（聊天容器内存在 data-chat-flow-region）：浮窗 portal 到 body，
 *   水平跟随圆环并夹在流区左右边界内，垂直贴流区底部、不越出上下边界；
 * - 流区过矮/过窄：浮窗收缩到可用尺寸并在内部滚动，不越出流区；
 * - 流区尺寸变化（窗口缩放/输入区展开）：重新夹紧；
 * - 非聊天场景（无消息流区）：回退锚点上方左对齐的旧定位；
 * - 受限定位下鼠标离开圆环给移入宽限（浮窗与圆环之间隔着输入区），不立即闪关。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ContextUsageIndicator } from '../ContextUsageIndicator'

interface FakeRect {
  top: number
  bottom: number
  left: number
  right: number
  width: number
  height: number
}

const ZERO_RECT: FakeRect = { top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }

/** 按需补全的四边矩形（宽高可由对边/自身推得） */
function fakeRect(partial: Partial<FakeRect>): FakeRect {
  const left = partial.left ?? 0
  const top = partial.top ?? 0
  const width = partial.width ?? (partial.right ?? 0) - left
  const height = partial.height ?? (partial.bottom ?? 0) - top
  return {
    top,
    left,
    width,
    height,
    right: partial.right ?? left + width,
    bottom: partial.bottom ?? top + height,
  }
}

let flowRect = ZERO_RECT
let anchorRect = ZERO_RECT
let panelRect = ZERO_RECT

beforeEach(() => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    if (this.hasAttribute('data-chat-flow-region')) return flowRect as DOMRect
    const testId = this.getAttribute('data-testid')
    if (testId === 'context-usage-indicator') return anchorRect as DOMRect
    if (testId === 'context-usage-popover-panel') return panelRect as DOMRect
    return ZERO_RECT as DOMRect
  })
})

afterEach(() => {
  vi.restoreAllMocks()
})

/** 聊天场景：消息流区与输入区同属 chat-container（流区是输入区上方的兄弟区域） */
function renderInChat() {
  return render(
    <div data-testid="chat-container">
      <div data-chat-flow-region />
      <ContextUsageIndicator modelName="m" currentTokenUsage={1000} maxTokens={2000} />
    </div>,
  )
}

function openPopover() {
  fireEvent.mouseEnter(screen.getByTestId('context-usage-indicator'))
}

describe('ContextUsageIndicator — 浮窗定位约束', () => {
  it('聊天场景：浮窗 portal 到 body，贴消息流区底部、水平跟随圆环', () => {
    flowRect = fakeRect({ top: 100, bottom: 500, left: 50, right: 850 })
    anchorRect = fakeRect({ top: 600, bottom: 632, left: 120, width: 100 })
    panelRect = fakeRect({ width: 256, height: 300 })

    renderInChat()
    openPopover()

    const popover = screen.getByTestId('context-usage-popover')
    // portal 到 body：不被消息流/输入区祖先的 overflow 裁剪
    expect(popover.parentElement).toBe(document.body)
    expect(popover.className).toContain('fixed')
    // 底边夹到流区底边内（500-8=492），浮窗高 300 → top=192
    expect(popover.style.left).toBe('120px')
    expect(popover.style.top).toBe('192px')

    const panel = screen.getByTestId('context-usage-popover-panel')
    expect(panel.style.width).toBe('256px')
    expect(panel.style.maxHeight).toBe('300px')
  })

  it('圆环靠右：浮窗左移，不越出消息流区右边界', () => {
    flowRect = fakeRect({ top: 100, bottom: 500, left: 50, right: 850 })
    anchorRect = fakeRect({ top: 600, left: 700, width: 100 })
    panelRect = fakeRect({ width: 256, height: 200 })

    renderInChat()
    openPopover()

    // 850-8-256=586
    expect(screen.getByTestId('context-usage-popover').style.left).toBe('586px')
  })

  it('消息流区过矮：浮窗收缩到区内可用高度并内部滚动，不越出上下边界', () => {
    flowRect = fakeRect({ top: 100, bottom: 300, left: 50, right: 850 })
    anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
    panelRect = fakeRect({ width: 256, height: 400 })

    renderInChat()
    openPopover()

    const popover = screen.getByTestId('context-usage-popover')
    const panel = screen.getByTestId('context-usage-popover-panel')
    // 可用高度 = 300-100-16 = 184；贴顶 108，底边 292 < 流区底边 300
    expect(panel.style.maxHeight).toBe('184px')
    expect(popover.style.top).toBe('108px')
    expect(panel.className).toContain('overflow-y-auto')
  })

  it('消息流区尺寸变化（窗口缩放/输入区展开）：重新夹紧', () => {
    flowRect = fakeRect({ top: 100, bottom: 500, left: 50, right: 850 })
    anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
    panelRect = fakeRect({ width: 256, height: 300 })

    renderInChat()
    openPopover()
    expect(screen.getByTestId('context-usage-popover').style.top).toBe('192px')

    flowRect = fakeRect({ top: 100, bottom: 400, left: 50, right: 850 })
    act(() => {
      window.dispatchEvent(new Event('resize'))
    })

    expect(screen.getByTestId('context-usage-popover-panel').style.maxHeight).toBe('284px')
    expect(screen.getByTestId('context-usage-popover').style.top).toBe('108px')
  })

  it('无消息流区（侧栏等非聊天场景）：回退旧定位（锚点上方左对齐，不 portal）', () => {
    anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
    panelRect = fakeRect({ width: 256, height: 300 })

    render(<ContextUsageIndicator modelName="m" currentTokenUsage={1000} maxTokens={2000} />)
    openPopover()

    const popover = screen.getByTestId('context-usage-popover')
    expect(popover.className).toContain('bottom-full')
    expect(popover.parentElement).not.toBe(document.body)
  })

  it('受限定位：鼠标离开圆环后给移入宽限，不立即闪关', () => {
    vi.useFakeTimers()
    try {
      flowRect = fakeRect({ top: 100, bottom: 500, left: 50, right: 850 })
      anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
      panelRect = fakeRect({ width: 256, height: 300 })

      renderInChat()
      openPopover()

      fireEvent.mouseLeave(screen.getByTestId('context-usage-indicator'))
      expect(screen.getByTestId('context-usage-popover')).toBeInTheDocument()

      act(() => {
        vi.advanceTimersByTime(300)
      })
      expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('宽限期内重新移入：取消已排程的延迟关闭（原定时器到点不再关）', () => {
    vi.useFakeTimers()
    try {
      flowRect = fakeRect({ top: 100, bottom: 500, left: 50, right: 850 })
      anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
      panelRect = fakeRect({ width: 256, height: 300 })

      renderInChat()
      openPopover()

      // 离开排程延迟关闭 → 宽限期内重入取消定时器
      fireEvent.mouseLeave(screen.getByTestId('context-usage-indicator'))
      fireEvent.mouseEnter(screen.getByTestId('context-usage-indicator'))

      // 原延迟窗口走完：浮窗仍在（排程已被取消，不被旧定时器误关）
      act(() => {
        vi.advanceTimersByTime(300)
      })
      expect(screen.getByTestId('context-usage-popover')).toBeInTheDocument()

      // 再次离开才真正走延迟关闭
      fireEvent.mouseLeave(screen.getByTestId('context-usage-indicator'))
      act(() => {
        vi.advanceTimersByTime(300)
      })
      expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('流区存在但零尺寸：布局置空回退旧定位（不按无效矩形夹紧）', () => {
    // 流区在 DOM 上可找到（data-chat-flow-region），但实测宽高为 0（未布局/隐藏）
    flowRect = ZERO_RECT
    anchorRect = fakeRect({ top: 600, left: 120, width: 100 })
    panelRect = fakeRect({ width: 256, height: 300 })

    renderInChat()
    openPopover()

    // 无有效布局 → 回退锚点上方左对齐旧定位（不 portal、无内联夹紧坐标）
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover.className).toContain('bottom-full')
    expect(popover.parentElement).not.toBe(document.body)
  })
})
