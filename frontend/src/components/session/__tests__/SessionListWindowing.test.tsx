// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * 会话列表窗口化（虚拟滚动）契约测试（renderer 内存优化项 1）
 *
 * - 超过阈值（>40 条）："全部会话"组仅渲染可视窗口±缓冲，总高度以占位撑出
 *   （滚动条长度不缩水）；置顶组、阈值以下全量渲染（既有契约不回退）
 * - 滚动换窗：滚到底部后窗口滑向末尾（首屏外条目卸载、尾部条目挂载）
 * - fail-open：无布局尺寸（clientHeight=0）时全量渲染，不因误判而丢内容
 * - 窗口化下点击/星标交互不回退
 *
 * jsdom 无布局，clientHeight/scrollTop 用 getter/setter 注入（同 MessageList 测试）。
 */
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SessionList } from '../SessionList'
import type { Session } from '@/types/models'

function createSession(i: number, overrides: Partial<Session> = {}): Session {
  return {
    id: `session-${i}`,
    title: `会话标题${i}`,
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-02T00:00:00Z',
    messageCount: 1,
    starred: false,
    pinned: false,
    ...overrides,
  }
}

const callbacks = {
  onSessionClick: vi.fn(),
  onDeleteSession: vi.fn().mockResolvedValue(undefined),
  onEditSession: vi.fn(),
  onCopySession: vi.fn(),
  onStarSession: vi.fn(),
  onPinSession: vi.fn(),
}

/**
 * 注入滚动容器布局尺寸（jsdom 无布局引擎）
 *
 * 同时按真实几何语义模拟 getBoundingClientRect：viewportTop(容器) = -scrollTop、
 * viewportTop(占位) = 内容偏移(0) - scrollTop——两者之差即占位在内容中的偏移，
 * 与浏览器一致地不随 scrollTop 漂移。
 */
function mockScrollMetrics(el: HTMLElement, clientHeight: number) {
  let currentScrollTop = 0
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(el, 'scrollHeight', {
    configurable: true,
    get: () => 120 * 55,
  })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => currentScrollTop,
    set: (v: number) => {
      currentScrollTop = v
    },
  })
  el.getBoundingClientRect = (() => ({ top: -currentScrollTop } as DOMRect)) as typeof el.getBoundingClientRect
  const spacer = el.querySelector('[data-testid="session-list-window"]') as HTMLElement | null
  if (spacer) {
    // 真实几何：viewportTop(list) = 内容偏移(0) + viewportTop(容器) - scrollTop
    spacer.getBoundingClientRect = (() =>
      ({ top: -2 * currentScrollTop }) as DOMRect) as typeof spacer.getBoundingClientRect
  }
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('会话列表窗口化', () => {
  it('超阈值（120 条）仅渲染可视窗口±缓冲，占位撑出完整滚动高度', () => {
    const sessions = Array.from({ length: 120 }, (_, i) => createSession(i))
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    const scroller = container.querySelector('[data-testid="session-list-scroll"]') as HTMLElement
    mockScrollMetrics(scroller, 600)
    // 布局尺寸就绪后的首次滚动触发窗口重算（对应浏览器中挂载即有正确尺寸）
    fireEvent.scroll(scroller)

    const rendered = screen.getAllByRole('button', { name: /^会话:/ })
    // 窗口 + overscan 有界：远小于全量 120，且覆盖一屏（600/55≈11 条）
    expect(rendered.length).toBeLessThan(40)
    expect(rendered.length).toBeGreaterThanOrEqual(11)
    // 占位撑出完整滚动高度：滚动条长度不因窗口化缩水
    const spacer = container.querySelector('[data-testid="session-list-window"]') as HTMLElement
    expect(spacer.style.height).toBe(`${120 * 55}px`)
  })

  it('滚动到底部：窗口滑向末尾（尾部条目挂载、首屏条目卸载）', () => {
    const sessions = Array.from({ length: 120 }, (_, i) => createSession(i))
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    const scroller = container.querySelector('[data-testid="session-list-scroll"]') as HTMLElement
    mockScrollMetrics(scroller, 600)
    // 布局尺寸就绪后的首次滚动触发窗口重算（对应浏览器中挂载即有正确尺寸）
    fireEvent.scroll(scroller)

    // 顶部：首条在窗、末条不在
    expect(screen.getByText('会话标题0')).toBeInTheDocument()
    expect(screen.queryByText('会话标题119')).not.toBeInTheDocument()

    act(() => {
      scroller.scrollTop = 120 * 55 - 600
      fireEvent.scroll(scroller)
    })

    expect(screen.getByText('会话标题119')).toBeInTheDocument()
    expect(screen.queryByText('会话标题0')).not.toBeInTheDocument()
  })

  it('阈值以下（≤40 条）全量渲染：既有契约不回退', () => {
    const sessions = Array.from({ length: 40 }, (_, i) => createSession(i))
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    // 无占位（未启用窗口化）
    expect(container.querySelector('[data-testid="session-list-window"]')).toBeNull()
    expect(screen.getAllByRole('button', { name: /^会话:/ })).toHaveLength(40)
  })

  it('置顶会话不受窗口化影响（分组照常全量渲染）', () => {
    const sessions = [
      createSession(-1, { id: 'pinned-1', title: '置顶会话', pinned: true }),
      ...Array.from({ length: 100 }, (_, i) => createSession(i)),
    ]
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    expect(screen.getByText('置顶会话')).toBeInTheDocument()
    const pinnedGroup = screen.getByText('已置顶').closest('[data-group="pinned"]')
    expect(within(pinnedGroup as HTMLElement).getByText('置顶会话')).toBeInTheDocument()
    // 普通组仍窗口化
    expect(container.querySelector('[data-testid="session-list-window"]')).not.toBeNull()
  })

  it('无布局尺寸（clientHeight=0）fail-open 全量渲染', () => {
    const sessions = Array.from({ length: 120 }, (_, i) => createSession(i))
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    const scroller = container.querySelector('[data-testid="session-list-scroll"]') as HTMLElement
    mockScrollMetrics(scroller, 0)

    expect(screen.getAllByRole('button', { name: /^会话:/ })).toHaveLength(120)
  })

  it('窗口化下可见条目交互不回退：点击切会话、星标回调', () => {
    const sessions = Array.from({ length: 120 }, (_, i) => createSession(i))
    const { container } = render(
      <SessionList
        sessions={sessions}
        activeSessionId={null}
        deletingSessionIds={new Set()}
        {...callbacks}
      />,
    )
    const scroller = container.querySelector('[data-testid="session-list-scroll"]') as HTMLElement
    mockScrollMetrics(scroller, 600)

    fireEvent.click(screen.getByText('会话标题3'))
    expect(callbacks.onSessionClick).toHaveBeenCalledWith('session-3')

    fireEvent.click(screen.getAllByRole('button', { name: /^星标/ })[0])
    expect(callbacks.onStarSession).toHaveBeenCalledTimes(1)
  })
})
