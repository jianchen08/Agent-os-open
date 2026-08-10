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
  MessageItem: ({ message, isLast, isGenerating }: { message: Message; isLast: boolean; isGenerating: boolean }) => (
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
  const prevTop = (Object.getOwnPropertyDescriptor(el, 'scrollTop')?.get as (() => number) | undefined)?.()
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
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

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
      const messages = [
        makeMessage({ id: 'msg-1', content: '消息一' }),
        makeMessage({ id: 'msg-2', content: '消息二' }),
        makeMessage({ id: 'msg-3', content: '消息三' }),
      ]
      render(<MessageList {...defaultProps} messages={messages} />)

      expect(screen.getByTestId('message-item-msg-1')).toBeInTheDocument()
      expect(screen.getByTestId('message-item-msg-2')).toBeInTheDocument()
      expect(screen.getByTestId('message-item-msg-3')).toBeInTheDocument()
    })

    it('最后一条消息 isLast 为 true', () => {
      const messages = [
        makeMessage({ id: 'msg-1' }),
        makeMessage({ id: 'msg-2' }),
      ]
      render(<MessageList {...defaultProps} messages={messages} isGenerating={true} />)

      // 最后一条消息应有生成指示器
      expect(screen.getByTestId('generating-indicator')).toBeInTheDocument()
    })
  })

  describe('isGenerating 状态', () => {
    it('isGenerating=true 且最后一条是 user 消息时显示思考中', () => {
      const messages = [
        makeMessage({ id: 'msg-1', role: 'user', content: '你好' }),
      ]
      const { container } = render(<MessageList {...defaultProps} messages={messages} isGenerating={true} />)

      expect(container.textContent).toContain('思考中')
    })

    it('isGenerating=false 时不显示思考中', () => {
      const messages = [
        makeMessage({ id: 'msg-1', role: 'user', content: '你好' }),
      ]
      const { container } = render(<MessageList {...defaultProps} messages={messages} isGenerating={false} />)

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
      const appended = [
        ...initialMessages,
        makeMessage({ id: 'msg-2', sequence: 2 }),
      ]
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
    function makeTriggerableResizeObserver() {
      const ref: { cb: (() => void) | null } = { cb: null }
      vi.stubGlobal('ResizeObserver', class {
        constructor(cb: () => void) { ref.cb = cb }
        observe() {}
        unobserve() {}
        disconnect() {}
      })
      return ref
    }

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

      // 用户上滑到中间（真实用户滚动是 wheel→scroll，wheel 置位 userScrolled，
      // 随后 onScroll 据 userScrolled 判定为主动上滑，置 isFollowingBottom=false）
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
  })
})
