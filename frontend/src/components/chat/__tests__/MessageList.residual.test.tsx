/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * MessageList 残余覆盖补测：
 * - 多代切换器锚点：同 base_seq 有两段时，轮末 assistant 行收到锚点（‹i/n› 可切换）
 * - 锚点恢复竞态：恢复帧内消息行被重建（锚点行消失）→ 放弃校正、不抛错
 * - 锚点恢复收敛上限：偏移始终不收敛时 30 帧后停止校正（不无限 rAF）
 * - 「回到底部」在无 element.scrollTo 的环境（老 WebView/jsdom）直接钉底
 *
 * mock 边界：MessageItem 替身（重依赖链，且本文件只关心列表滚动/锚点装配）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'
import type { Message } from '@/types/models'

vi.mock('../MessageItem', () => ({
  MessageItem: ({
    message,
    segmentSwitcherBaseSeq,
  }: {
    message: Message
    segmentSwitcherBaseSeq?: number | null
  }) => (
    <div data-testid={`message-item-${message.id}`} data-anchor={String(segmentSwitcherBaseSeq ?? '')}>
      {message.content}
    </div>
  ),
}))

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'sess-1',
    sequence: 1,
    role: 'assistant',
    content: '内容',
    timestamp: '2026-10-28T00:00:00Z',
    status: 'completed',
    ...overrides,
  }
}

const baseProps: ExtendedMessageListProps = { messages: [], className: '' }

/** rAF 队列：手动 flush，便于精确驱动逐帧收敛逻辑 */
let rafQueue: FrameRequestCallback[] = []
function flushFrames(times = 1) {
  for (let i = 0; i < times; i += 1) {
    const pending = rafQueue
    rafQueue = []
    for (const cb of pending) cb(0)
  }
}

/** 给元素注入可控滚动度量（jsdom 无布局） */
function mockMetrics(el: HTMLElement, scrollHeight: number, clientHeight = 200) {
  let top = 0
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => top,
    set: (v: number) => {
      top = v
    },
  })
  return el
}

function rectOf(top: number, bottom: number): DOMRect {
  return {
    x: 0,
    y: top,
    left: 0,
    top,
    right: 400,
    bottom,
    width: 400,
    height: bottom - top,
    toJSON: () => ({}),
  } as DOMRect
}

beforeEach(() => {
  rafQueue = []
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafQueue.push(cb)
    return 0
  })
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  usePipelineMessageStore.setState({ activePipelineId: null, spanIndex: {} } as never)
})

afterEach(() => {
  vi.unstubAllGlobals()
  usePipelineMessageStore.setState({ activePipelineId: null, spanIndex: {} } as never)
})

describe('MessageList — 多代切换器锚点', () => {
  it('同 base_seq 存在两段 → 覆盖范围内轮末 assistant 行收到锚点', () => {
    usePipelineMessageStore.setState({
      activePipelineId: 'pipe-seg',
      spanIndex: {
        'pipe-seg': [
          { base_seq: 1, segment_id: 'gen-a' },
          { base_seq: 1, segment_id: 'gen-b' },
        ],
      },
    } as never)

    render(
      <MessageList
        {...baseProps}
        messages={[
          makeMessage({ id: 'm1', sequence: 1 }),
          makeMessage({ id: 'm2', sequence: 2, role: 'user' }),
          makeMessage({ id: 'm3', sequence: 3 }),
        ]}
      />,
    )

    // 轮末 assistant（sequence 3）拿到锚点；user 行不参与
    expect(screen.getByTestId('message-item-m3')).toHaveAttribute('data-anchor', '1')
    expect(screen.getByTestId('message-item-m2')).toHaveAttribute('data-anchor', '')
  })

  it('段数不足两段 → 无锚点（单代不显示切换器）', () => {
    usePipelineMessageStore.setState({
      activePipelineId: 'pipe-seg',
      spanIndex: { 'pipe-seg': [{ base_seq: 1, segment_id: 'gen-a' }] },
    } as never)

    render(<MessageList {...baseProps} messages={[makeMessage({ id: 'm1', sequence: 1 })]} />)

    expect(screen.getByTestId('message-item-m1')).toHaveAttribute('data-anchor', '')
  })
})

/** 制造一次「翻历史后切走」的锚点缓存：先上滑采样锚点，再卸载 */
function seedAnchorCache(tabId: string, messages: Message[]): void {
  const { container, unmount } = render(
    <MessageList {...baseProps} messages={messages} tabId={tabId} />,
  )
  const listEl = mockMetrics(
    container.querySelector('[data-testid="message-list"]') as HTMLElement,
    1000,
  )
  flushFrames()
  // 视口顶压住首行：首行 bottom 越过容器顶 → 采样为该 Tab 的恢复锚点
  listEl.getBoundingClientRect = () => rectOf(0, 200)
  const firstRow = container.querySelector('[data-msg-id]') as HTMLElement
  firstRow.getBoundingClientRect = () => rectOf(-50, 150)
  listEl.scrollTop = 100
  fireEvent.scroll(listEl)
  flushFrames()
  unmount()
}

describe('MessageList — 锚点恢复的竞态与收敛上限', () => {
  it('恢复帧内锚点行被重建 → 放弃校正且保持原滚动位置', () => {
    const messages = [makeMessage({ id: 'a1', sequence: 1 }), makeMessage({ id: 'a2', sequence: 2 })]
    seedAnchorCache('anchor-race', messages)

    const { container, rerender } = render(
      <MessageList {...baseProps} messages={messages} tabId="anchor-race" />,
    )
    const listEl = mockMetrics(
      container.querySelector('[data-testid="message-list"]') as HTMLElement,
      1000,
    )
    // 外层恢复帧排队（内部 restoreByAnchor 又排一帧）
    flushFrames()
    const topBefore = listEl.scrollTop

    // 恢复帧执行前消息被重建：锚点行 id 消失
    rerender(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'b1', sequence: 1 })]}
        tabId="anchor-race"
      />,
    )
    flushFrames()

    expect(listEl.scrollTop).toBe(topBefore)
  })

  it('偏移始终不收敛 → 30 帧后停止校正（不无限排帧）', () => {
    const messages = [makeMessage({ id: 'c1', sequence: 1 })]
    seedAnchorCache('anchor-cap', messages)

    const { container } = render(
      <MessageList {...baseProps} messages={messages} tabId="anchor-cap" />,
    )
    const listEl = mockMetrics(
      container.querySelector('[data-testid="message-list"]') as HTMLElement,
      1000,
    )
    listEl.getBoundingClientRect = () => rectOf(0, 200)
    const row = container.querySelector('[data-msg-id]') as HTMLElement
    // 行顶与缓存偏移恒差 50px → 每帧校正后仍不收敛
    row.getBoundingClientRect = () => rectOf(0, 200)

    flushFrames(31)
    const capped = listEl.scrollTop
    flushFrames(5)

    expect(listEl.scrollTop).toBe(capped)
    expect(rafQueue).toHaveLength(0)
  })
})

describe('MessageList — 回到底部（无 scrollTo 环境）', () => {
  it('上滑离底后点浮钮 → 直接钉回底部', () => {
    const { container } = render(
      <MessageList {...baseProps} messages={[makeMessage({ id: 'd1', sequence: 1 })]} />,
    )
    const listEl = mockMetrics(
      container.querySelector('[data-testid="message-list"]') as HTMLElement,
      1000,
    )
    flushFrames()
    // 无 element.scrollTo 的宿主（老 WebView / jsdom）
    Object.defineProperty(listEl, 'scrollTo', { value: undefined, configurable: true })
    listEl.scrollTop = 300
    fireEvent.scroll(listEl)

    fireEvent.click(screen.getByTestId('message-list-scroll-to-bottom'))

    expect(listEl.scrollTop).toBe(1000)
  })
})
