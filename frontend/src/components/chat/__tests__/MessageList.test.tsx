/**
 * MessageList 组件测试
 *
 * 验证消息列表的渲染逻辑：
 * - 空消息列表显示占位符
 * - 有消息时正确渲染消息项
 * - isGenerating 状态下显示思考中提示
 * - 传入不同 props 的渲染行为
 * - 首次钉底、切 Tab 缓存恢复、底部追加跟随
 *
 * 注：浏览器原生 overflow-anchor（加载更多不跳）是纯 CSS，jsdom 不实现 CSS 引擎，
 * 这部分靠浏览器实测，单测不覆盖。
 */

import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'
import type { Message } from '@/types/models'

// Mock MessageItem（避免深入渲染依赖）
vi.mock('../MessageItem', () => ({
  MessageItem: ({
    message,
    isLast,
    isGenerating,
  }: {
    message: Message
    isLast: boolean
    isGenerating: boolean
  }) => (
    <div data-testid={`message-item-${message.id}`}>
      <span>{message.content}</span>
      {isGenerating && isLast && <span data-testid="generating-indicator">生成中</span>}
    </div>
  ),
}))

/** 创建测试用 Message 对象 */
function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-001',
    sessionId: 'sess-001',
    sequence: 1,
    role: 'assistant',
    content: '测试消息内容',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

/**
 * 给 DOM 元素打补丁，模拟滚动尺寸
 *
 * jsdom 默认 scrollHeight/scrollTop 为 0 且写入 scrollTop 不生效，
 * 用 getter/setter 覆盖以便断言 MessageList 的滚动逻辑。
 */
function mockScrollMetrics(el: HTMLElement, scrollHeight: number, clientHeight = 200) {
  // 保留已有 scrollTop：更新 scrollHeight 时不应重置滚动位置
  const prevTop = (
    Object.getOwnPropertyDescriptor(el, 'scrollTop')?.get as (() => number) | undefined
  )?.()
  let currentScrollTop = prevTop ?? 0
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => scrollHeight })
  Object.defineProperty(el, 'clientHeight', { configurable: true, get: () => clientHeight })
  Object.defineProperty(el, 'scrollTop', {
    configurable: true,
    get: () => currentScrollTop,
    set: (v: number) => {
      currentScrollTop = v
    },
  })
  return el
}

/**
 * requestAnimationFrame polyfill
 *
 * MessageList 用 rAF 异步设置 scrollTop。jsdom 不提供，测试里进队列后手动 flush，
 * 贴近真实异步行为且断言可控。
 */
let rafQueue: FrameRequestCallback[] = []
function flushRaf() {
  const pending = rafQueue
  rafQueue = []
  for (const cb of pending) cb(0)
}

beforeEach(() => {
  rafQueue = []
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafQueue.push(cb)
    return 0
  })
  // MessageList 首次加载用 ResizeObserver 持续校正钉底，jsdom 不提供，需 polyfill
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

/**
 * 可手动触发回调的 ResizeObserver mock
 *
 * jsdom 不实现 ResizeObserver，beforeEach stub 的是空实现（验证不了"内容变化
 * 触发钉底"）。需要手动触发回调的场景用它模拟内容容器尺寸变化。
 */
function makeTriggerableResizeObserver() {
  const ref: { cb: (() => void) | null } = { cb: null }
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(cb: () => void) {
        ref.cb = cb
      }
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
  return ref
}

describe('MessageList', () => {
  const defaultProps: ExtendedMessageListProps = {
    messages: [],
    isGenerating: false,
    modelName: 'test-model',
    className: '',
  }

  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('空消息列表', () => {
    it('显示空状态占位符', () => {
      render(<MessageList {...defaultProps} messages={[]} />)
      expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
    })

    it('空状态包含引导文案', () => {
      render(<MessageList {...defaultProps} messages={[]} />)
      expect(screen.getByText('开始新的对话')).toBeInTheDocument()
      expect(screen.getByText(/发送消息开始与 AI 助手交流/)).toBeInTheDocument()
    })
  })

  describe('有消息的列表', () => {
    it('渲染消息列表容器', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(screen.getByTestId('message-list')).toBeInTheDocument()
    })

    it('渲染多条消息', () => {
      // 用交替 role（user/assistant/user）避免连续 assistant 被渲染层
      // mergeConsecutiveAssistantMessages 合并成 1 条气泡，真正验证"渲染多条"。
      const messages = [
        makeMessage({ id: 'msg-1', role: 'user', content: '消息一' }),
        makeMessage({ id: 'msg-2', role: 'assistant', content: '消息二' }),
        makeMessage({ id: 'msg-3', role: 'user', content: '消息三' }),
      ]
      render(<MessageList {...defaultProps} messages={messages} />)

      expect(screen.getByTestId('message-item-msg-1')).toBeInTheDocument()
      expect(screen.getByTestId('message-item-msg-2')).toBeInTheDocument()
      expect(screen.getByTestId('message-item-msg-3')).toBeInTheDocument()
    })

    it('事件乱序到达仍按 sequence 时序渲染（DSH orderedVisible 同款兜底）', () => {
      // 模拟事件到达顺序 ≠ 时序：seq=3 先到、seq=1/2 后到（且时间戳不同步）
      const messages = [
        makeMessage({ id: 'm3', role: 'user', content: '第三条（先到）', sequence: 3, timestamp: new Date(Date.now() + 1000).toISOString() }),
        makeMessage({ id: 'm1', role: 'user', content: '第一条', sequence: 1 }),
        makeMessage({ id: 'm2', role: 'assistant', content: '第二条', sequence: 2 }),
      ]
      const { container } = render(<MessageList {...defaultProps} messages={messages} />)

      const order = [...container.querySelectorAll('[data-msg-id]')].map((el) => el.getAttribute('data-msg-id'))
      expect(order).toEqual(['m1', 'm2', 'm3'])
    })

    it('无 sequence 消息按 timestamp 排序，且排在有 sequence 消息之后', () => {
      const base = Date.now()
      const messages = [
        makeMessage({ id: 'no-seq-late', role: 'user', content: '无seq晚', sequence: undefined, timestamp: new Date(base + 5000).toISOString() }),
        makeMessage({ id: 's2', role: 'assistant', content: 'seq2', sequence: 2, timestamp: new Date(base).toISOString() }),
        makeMessage({ id: 's1', role: 'user', content: 'seq1', sequence: 1, timestamp: new Date(base).toISOString() }),
        makeMessage({ id: 'no-seq-early', role: 'user', content: '无seq早', sequence: undefined, timestamp: new Date(base + 1000).toISOString() }),
      ]
      const { container } = render(<MessageList {...defaultProps} messages={messages} />)

      const order = [...container.querySelectorAll('[data-msg-id]')].map((el) => el.getAttribute('data-msg-id'))
      expect(order).toEqual(['s1', 's2', 'no-seq-early', 'no-seq-late'])
    })

    it('多轮工具调用消息流渲染：user + 单一合并 assistant 气泡（回归）', () => {
      // 对应用户反馈 bug：多轮工具调用中 AI 消息只剩一条、tool 消息不显示。
      // 现行契约（与流式渲染同构）：mergeConsecutiveAssistantMessages 把一个对话轮次
      // （assistant 声明 → tool 结果 → assistant 回答 → …）合并为单一 assistant 气泡，
      // tool 结果注入对应 tool_call part（part 层渲染），不再产出独立 tool 消息项。
      const messages = [
        makeMessage({ id: 'u1', role: 'user', content: '查天气', sequence: 1 }),
        makeMessage({
          id: 'a1',
          role: 'assistant',
          content: '',
          sequence: 2,
          parts: [
            {
              type: 'tool_call',
              callId: 'tc-1',
              name: 'get_weather',
              args: {},
              state: 'done',
              sequence: 0,
            },
          ] as any,
        }),
        makeMessage({
          id: 't1',
          role: 'tool',
          content: '北京晴',
          sequence: 3,
          toolCallId: 'tc-1',
          toolName: 'get_weather',
          toolResult: '北京晴',
        }),
        makeMessage({
          id: 'a2',
          role: 'assistant',
          content: '今天晴，查明天？',
          sequence: 4,
          parts: [
            { type: 'text', content: '今天晴，查明天？', state: 'done', sequence: 0 },
            {
              type: 'tool_call',
              callId: 'tc-2',
              name: 'get_weather',
              args: {},
              state: 'done',
              sequence: 1,
            },
          ] as any,
        }),
        makeMessage({
          id: 't2',
          role: 'tool',
          content: '明天多云',
          sequence: 5,
          toolCallId: 'tc-2',
          toolName: 'get_weather',
          toolResult: '明天多云',
        }),
        makeMessage({
          id: 'a3',
          role: 'assistant',
          content: '明天多云，带外套',
          sequence: 6,
          parts: [{ type: 'text', content: '明天多云，带外套', state: 'done', sequence: 0 }] as any,
        }),
      ]
      render(<MessageList {...defaultProps} messages={messages} />)

      // ★ 现行契约：u1 + 合并后的单一 assistant 气泡（以组内首条 a1 为载体）
      expect(screen.getByTestId('message-item-u1')).toBeInTheDocument()
      expect(screen.getByTestId('message-item-a1')).toBeInTheDocument()
      // tool / 后续 assistant 不再作为独立消息项渲染（被合并进 a1 的 parts）
      expect(screen.queryByTestId('message-item-t1')).not.toBeInTheDocument()
      expect(screen.queryByTestId('message-item-a2')).not.toBeInTheDocument()
      expect(screen.queryByTestId('message-item-t2')).not.toBeInTheDocument()
      expect(screen.queryByTestId('message-item-a3')).not.toBeInTheDocument()

      // 合并气泡包含各轮 assistant 的文本内容（content 以空行拼接为一个文本节点）
      const mergedBubble = screen.getByTestId('message-item-a1')
      expect(mergedBubble).toHaveTextContent('今天晴，查明天？')
      expect(mergedBubble).toHaveTextContent('明天多云，带外套')
    })

    it('最后一条消息 isLast 为 true', () => {
      const messages = [makeMessage({ id: 'msg-1' }), makeMessage({ id: 'msg-2' })]
      render(<MessageList {...defaultProps} messages={messages} isGenerating={true} />)

      // 最后一条消息应有生成指示器
      expect(screen.getByTestId('generating-indicator')).toBeInTheDocument()
    })
  })

  describe('isGenerating 状态', () => {
    it('isGenerating=true 且最后一条是 user 消息时显示思考中', () => {
      const messages = [makeMessage({ id: 'msg-1', role: 'user', content: '你好' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} isGenerating={true} />,
      )

      // displayMessages 渲染 1 条 user 消息；最后一条 role='user' 且 isGenerating → 显示"思考中"
      expect(container.textContent).toContain('思考中')
    })

    it('isGenerating=false 时不显示思考中', () => {
      const messages = [makeMessage({ id: 'msg-1', role: 'user', content: '你好' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} isGenerating={false} />,
      )

      expect(container.textContent).not.toContain('思考中')
    })
  })

  describe('自定义 props', () => {
    it('className 被传递到容器', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} className="custom-class" />,
      )
      const listEl = container.querySelector('.custom-class')
      expect(listEl).toBeInTheDocument()
    })

    it('modelName 传递到 MessageItem', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} modelName="gpt-4" />)
      // MessageItem mock 渲染了消息内容
      expect(screen.getByTestId('message-item-msg-1')).toBeInTheDocument()
    })
  })

  describe('滚动行为', () => {
    afterEach(() => {
      cleanup()
    })

    it('无缓存时首次加载钉到最底部', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} tabId="no-cache" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()

      expect(listEl.scrollTop).toBe(1000)
    })

    it('卸载后重新挂载同一 Tab 恢复缓存的滚动位置', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const tabId = 'restore'

      // 第一次挂载：无缓存 → 钉到底部（1000）
      const { container, unmount } = render(
        <MessageList {...defaultProps} messages={messages} tabId={tabId} />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 模拟用户向上滚动到中间（需派发 scroll 事件，onScroll 才会记录 scrollTop）
      listEl.scrollTop = 400
      fireEvent.scroll(listEl)

      // 卸载：触发 cleanup 写入缓存
      unmount()

      // 重新挂载同一 Tab
      const { container: container2 } = render(
        <MessageList {...defaultProps} messages={messages} tabId={tabId} />,
      )
      const listEl2 = container2.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl2, 1000)
      flushRaf()

      // 恢复到缓存的 400
      expect(listEl2.scrollTop).toBe(400)
    })

    it('切换到不同 Tab 不受其他 Tab 缓存影响', () => {
      const messages = [makeMessage({ id: 'msg-1' })]

      // Tab A 滚到中间后卸载
      const { container, unmount } = render(
        <MessageList {...defaultProps} messages={messages} tabId="tab-A" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      listEl.scrollTop = 300
      fireEvent.scroll(listEl)
      unmount()

      // 切到全新的 Tab B：无缓存 → 钉到底
      const { container: container2 } = render(
        <MessageList {...defaultProps} messages={messages} tabId="tab-B" />,
      )
      const listEl2 = container2.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl2, 1000)
      flushRaf()

      expect(listEl2.scrollTop).toBe(1000)
    })

    it('底部追加新消息时跟随到底部', () => {
      const initialMessages = [makeMessage({ id: 'msg-1', sequence: 1 })]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={initialMessages} tabId="append" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 追加新消息（底部）
      const appended = [...initialMessages, makeMessage({ id: 'msg-2', sequence: 2 })]
      mockScrollMetrics(listEl, 1200)
      rerender(<MessageList {...defaultProps} messages={appended} tabId="append" />)
      flushRaf()

      // 跟随到底部（1200）
      expect(listEl.scrollTop).toBe(1200)
    })

    /**
     * 可手动触发回调的 ResizeObserver mock
     *
     * jsdom 不实现 ResizeObserver，beforeEach stub 的是空实现（验证不了"内容变化
     * 触发钉底"）。这两个测试需要手动触发回调，模拟内容容器尺寸变化。
     */
    it('initFromAPI 重建（条数减少、内容变高）后，内容变化触发钉底回到底部', () => {
      // 复现 fix_20260629_enter_stuck_in_middle：进入页面 persist 钉底后，initFromAPI
      // 异步重建合并气泡使条数减少，原逻辑因"条数未增加"不钉底 → 停在中间。
      const ro = makeTriggerableResizeObserver()

      // 首次：3 条消息钉底（模拟 persist 快照恢复后挂载）
      const messages = [
        makeMessage({ id: 'msg-1', sequence: 1 }),
        makeMessage({ id: 'msg-2', sequence: 2 }),
        makeMessage({ id: 'msg-3', sequence: 3 }),
      ]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="rebuild" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 模拟 initFromAPI 重建：合并连续 assistant 后条数减少（3→1），单条内容更高
      const rebuilt = [makeMessage({ id: 'msg-merged', sequence: 2 })]
      rerender(<MessageList {...defaultProps} messages={rebuilt} tabId="rebuild" />)
      mockScrollMetrics(listEl, 1500)

      // 重建后条数减少，原"条数增加才钉底"逻辑不会触发；scrollTop 仍停在旧底部
      expect(listEl.scrollTop).toBe(1000)

      // 内容容器尺寸变化触发 contentResize observer → 钉回底部（修复后行为）
      ro.cb?.()
      expect(listEl.scrollTop).toBe(1500)
    })

    it('用户上滑后内容变化不钉底，不打扰翻历史', () => {
      const ro = makeTriggerableResizeObserver()

      const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="scroll-up" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 用户滚轮上滑到中间（wheel→浏览器滚动→scroll 事件，onScroll 按方向
      // 判定主动上滑，置 isFollowingBottom=false）
      fireEvent.wheel(listEl, { deltaY: -100 })
      listEl.scrollTop = 300
      fireEvent.scroll(listEl)

      // 内容变高（流式增长 / 重建）
      const grown = [...messages, makeMessage({ id: 'msg-2', sequence: 2 })]
      rerender(<MessageList {...defaultProps} messages={grown} tabId="scroll-up" />)
      mockScrollMetrics(listEl, 1200)

      ro.cb?.()

      // 不被拉回底部，停留在用户的滚动位置
      expect(listEl.scrollTop).toBe(300)
    })

    it('无手势上滑（滚动条拖拽/键盘滚动）后流式内容变化不钉底', () => {
      const ro = makeTriggerableResizeObserver()

      const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="drag-up" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 流式内容增长：跟随状态下 effect 排队 rAF 钉底
      const grown = [...messages, makeMessage({ id: 'msg-2', sequence: 2 })]
      rerender(<MessageList {...defaultProps} messages={grown} isGenerating tabId="drag-up" />)
      mockScrollMetrics(listEl, 1200)

      // 滚动条拖拽/键盘滚动没有 wheel/touchstart 手势事件，只有 scroll 事件——
      // 方向判定若依赖手势标记，用户会被流式钉底每帧拽回底部（无条件盯底）
      listEl.scrollTop = 300
      fireEvent.scroll(listEl)

      // 已排队的 rAF 钉底执行时跟随已停止，不落盘；ResizeObserver 同样不拉回
      flushRaf()
      ro.cb?.()

      expect(listEl.scrollTop).toBe(300)
    })

    it('内容收缩钳制 scrollTop 减小是程序性滚动，不停止跟随', () => {
      const ro = makeTriggerableResizeObserver()

      const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="clamp" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // initFromAPI 重建使内容变矮，浏览器钳制 scrollTop 到新的最大值（无手势）：
      // 程序性上移的落点在底部附近，onScroll 内随即恢复跟随（自愈），不停中间
      mockScrollMetrics(listEl, 700)
      listEl.scrollTop = 500
      fireEvent.scroll(listEl)

      // 跟随未被误停：后续内容变化仍钉回底部，不停中间
      const grown = [...messages, makeMessage({ id: 'msg-2', sequence: 2 })]
      rerender(<MessageList {...defaultProps} messages={grown} tabId="clamp" />)
      mockScrollMetrics(listEl, 900)
      ro.cb?.()

      expect(listEl.scrollTop).toBe(900)
    })

    it('用户上滑翻历史后滚回底部附近，恢复跟随', () => {
      const ro = makeTriggerableResizeObserver()

      const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="recover" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()

      // 上滑到中部 → 停止跟随
      listEl.scrollTop = 300
      fireEvent.scroll(listEl)
      // 滚回底部附近（距底 50px ≤ 150 阈值）→ 恢复跟随
      listEl.scrollTop = 750
      fireEvent.scroll(listEl)

      const grown = [...messages, makeMessage({ id: 'msg-2', sequence: 2 })]
      rerender(<MessageList {...defaultProps} messages={grown} tabId="recover" />)
      mockScrollMetrics(listEl, 1200)
      ro.cb?.()

      // 跟随已恢复：内容变化重新钉底
      expect(listEl.scrollTop).toBe(1200)
    })

    it('首次钉底轮询窗口内上滑，轮询终止不再强制钉底', () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
      try {
        const messages = [makeMessage({ id: 'msg-1' })]
        const { container } = render(
          <MessageList {...defaultProps} messages={messages} tabId="poll-stop" />,
        )
        const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
        mockScrollMetrics(listEl, 1000)
        flushRaf()
        expect(listEl.scrollTop).toBe(1000)

        // 无手势上滑（滚动条拖拽/键盘），scroll 事件方向判定停止跟随
        listEl.scrollTop = 300
        fireEvent.scroll(listEl)

        // 轮询到期：跟随已停止，不再强制钉底；轮询自终止后亦然
        vi.advanceTimersByTime(100)
        expect(listEl.scrollTop).toBe(300)
        vi.advanceTimersByTime(2000)
        expect(listEl.scrollTop).toBe(300)
      } finally {
        vi.useRealTimers()
      }
    })

    it('流式结束后仍在跟随，300ms 收尾钉底覆盖最终渲染高度', () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
      try {
        const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
        const { container, rerender } = render(
          <MessageList {...defaultProps} messages={messages} isGenerating tabId="end-pin" />,
        )
        const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
        mockScrollMetrics(listEl, 1000)
        flushRaf()
        expect(listEl.scrollTop).toBe(1000)

        // 流式结束，最终 markdown 渲染使内容变高（scrollTop 停在旧位置）
        rerender(
          <MessageList
            {...defaultProps}
            messages={messages}
            isGenerating={false}
            tabId="end-pin"
          />,
        )
        mockScrollMetrics(listEl, 1200)

        vi.advanceTimersByTime(400)

        // 收尾钉底落到最终底部
        expect(listEl.scrollTop).toBe(1200)
      } finally {
        vi.useRealTimers()
      }
    })

    it('流式结束后用户已上滑，300ms 收尾钉底放行用户位置', () => {
      vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] })
      try {
        const messages = [makeMessage({ id: 'msg-1', sequence: 1 })]
        const { container, rerender } = render(
          <MessageList {...defaultProps} messages={messages} isGenerating tabId="end-stay" />,
        )
        const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
        mockScrollMetrics(listEl, 1000)
        flushRaf()

        // 流式结束（跟随中 → 排队收尾钉底），收尾窗口内用户上滑翻历史
        rerender(
          <MessageList
            {...defaultProps}
            messages={messages}
            isGenerating={false}
            tabId="end-stay"
          />,
        )
        listEl.scrollTop = 300
        fireEvent.scroll(listEl)

        vi.advanceTimersByTime(400)

        // 收尾钉底不把视图拉回底部
        expect(listEl.scrollTop).toBe(300)
      } finally {
        vi.useRealTimers()
      }
    })
  })

  describe('消息定位跳转（jumpTarget）', () => {
    afterEach(() => {
      cleanup()
    })

    it('jumpTarget 命中已加载消息：滚动到目标（居中）、高亮并消费清除', () => {
      // 交替 role 避免连续 assistant 被合并成一条
      const messages = [
        makeMessage({ id: 'msg-1', sequence: 1, role: 'user' }),
        makeMessage({ id: 'msg-2', sequence: 2, role: 'assistant' }),
        makeMessage({ id: 'msg-3', sequence: 3, role: 'user' }),
      ]
      const onJumpConsumed = vi.fn()
      // 首轮无 jumpTarget 渲染（等布局就绪后 mock offsetTop 再注入 jumpTarget，
      // 避免 effect 在 jsdom 零布局下读到 offsetTop=0 提前消费）
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="jump-hit" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      // jsdom 无布局，锚点 offsetTop 恒 0——手工给定，验证居中计算
      const anchor = listEl.querySelector('[data-msg-id="msg-2"]') as HTMLElement
      Object.defineProperty(anchor, 'offsetTop', { configurable: true, value: 800 })

      rerender(
        <MessageList
          {...defaultProps}
          messages={messages}
          tabId="jump-hit"
          jumpTarget={{ sequence: 2 }}
          onJumpConsumed={onJumpConsumed}
        />,
      )
      flushRaf()

      // 首次定位钉底 RAF 先执行、跳转定位 RAF 后执行 → 最终停在目标（clientHeight=200 → 800-100）
      expect(listEl.scrollTop).toBe(700)
      expect(onJumpConsumed).toHaveBeenCalledTimes(1)
      // 目标消息被高亮标记
      expect(anchor.className).toContain('message-jump-highlight')
    })

    it('jumpTarget 未命中已加载消息：不滚动、不消费（留待翻页拉取后重试）', () => {
      const messages = [makeMessage({ id: 'msg-1', sequence: 1, role: 'user' })]
      const onJumpConsumed = vi.fn()
      const { container } = render(
        <MessageList
          {...defaultProps}
          messages={messages}
          tabId="jump-miss"
          jumpTarget={{ sequence: 99 }}
          onJumpConsumed={onJumpConsumed}
        />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()

      expect(onJumpConsumed).not.toHaveBeenCalled()
      // 不滚动（保持首次钉底位置）
      expect(listEl.scrollTop).toBe(1000)
    })

    it('定位后内容变化不钉底（停止跟随），用户停在目标位置', () => {
      const ro = makeTriggerableResizeObserver()

      const messages = [
        makeMessage({ id: 'msg-1', sequence: 1, role: 'user' }),
        makeMessage({ id: 'msg-2', sequence: 2, role: 'assistant' }),
      ]
      // 首轮无 jumpTarget 渲染，mock offsetTop 后再注入跳转目标
      const { container, rerender } = render(
        <MessageList {...defaultProps} messages={messages} tabId="jump-stay" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      const anchor = listEl.querySelector('[data-msg-id="msg-2"]') as HTMLElement
      Object.defineProperty(anchor, 'offsetTop', { configurable: true, value: 800 })

      rerender(
        <MessageList
          {...defaultProps}
          messages={messages}
          tabId="jump-stay"
          jumpTarget={{ sequence: 2 }}
          onJumpConsumed={() => {}}
        />,
      )
      flushRaf()
      expect(listEl.scrollTop).toBe(700)

      // 内容变高（新消息追加）触发 contentResize observer
      const grown = [...messages, makeMessage({ id: 'msg-3', sequence: 3, role: 'user' })]
      rerender(<MessageList {...defaultProps} messages={grown} tabId="jump-stay" />)
      mockScrollMetrics(listEl, 1200)
      ro.cb?.()

      // 定位已置 isFollowingBottom=false，内容变化不把视图拉回底部
      expect(listEl.scrollTop).toBe(700)
    })
  })
})
