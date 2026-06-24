/**
 * MessageList 组件测试
 *
 * 验证消息列表的渲染逻辑：
 * - 空消息列表显示占位符
 * - 有消息时正确渲染消息项
 * - isGenerating 状态下显示思考中提示
 * - 传入不同 props 的渲染行为
 * - 切换 Tab 卸载重建后滚动位置保存/恢复
 * - 内容高度异步变化时跟随底部不跳动（ResizeObserver 钉底）
 */

import { render, screen, cleanup } from '@testing-library/react'
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
  // 保留已有 scrollTop 值：更新 scrollHeight 时不应重置滚动位置
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
 * 全局 ResizeObserver / requestAnimationFrame polyfill
 *
 * MessageList 用 ResizeObserver 持续钉底、用 requestAnimationFrame 异步设置 scrollTop。
 * jsdom 不提供这两个 API。rAF 回调进队列，测试在 mock 好滚动尺寸后手动 flush，
 * 贴近真实异步行为且断言可控。
 */
let roCallbacks: ((entries: any[]) => void)[] = []
let rafQueue: FrameRequestCallback[] = []

/** 手动 flush 所有待执行的 requestAnimationFrame 回调 */
function flushRaf() {
  const pending = rafQueue
  rafQueue = []
  for (const cb of pending) cb(0)
}

/** 手动触发所有已注册的 ResizeObserver 回调 */
function triggerResize(target: HTMLElement) {
  for (const cb of roCallbacks) cb([{ target } as any])
}

beforeEach(() => {
  roCallbacks = []
  rafQueue = []
  vi.stubGlobal('ResizeObserver', class {
    constructor(cb: (entries: any[]) => void) {
      roCallbacks.push(cb)
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  })
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    rafQueue.push(cb)
    return 0
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

  describe('滚动位置保存/恢复', () => {
    afterEach(() => {
      cleanup()
    })

    it('无缓存时首次加载跟随底部（钉到 scrollHeight）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} tabId="scroll-no-cache" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)

      // 无缓存 → requestAnimationFrame 钉底，flush 后生效
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)
    })

    it('卸载后重新挂载同一 Tab 恢复缓存的滚动位置', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const tabId = 'scroll-restore'

      // 第一次挂载：无缓存 → 钉到底部（1000）
      const { container, unmount } = render(
        <MessageList {...defaultProps} messages={messages} tabId={tabId} />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 模拟用户向上滚动到中间
      listEl.scrollTop = 400
      expect(listEl.scrollTop).toBe(400)

      // 卸载：触发 cleanup 写入缓存
      unmount()

      // 重新挂载同一 Tab
      const { container: container2 } = render(
        <MessageList {...defaultProps} messages={messages} tabId={tabId} />,
      )
      const listEl2 = container2.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl2, 1000)
      // 有缓存 → rAF 恢复缓存位置
      flushRaf()

      // 恢复到缓存的 400，而不是重新钉到底部 1000
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

    it('内容高度变化且仍在跟随底部时，ResizeObserver 重新钉底（不跳动）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      const { container } = render(
        <MessageList {...defaultProps} messages={messages} tabId="resize-pin" />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      // 初始高度 1000，已钉底
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      expect(listEl.scrollTop).toBe(1000)

      // 模拟 Markdown/图片异步渲染撑高内容：scrollHeight 从 1000 变到 1500
      // 但 isFollowingBottom 仍为 true（用户没上滑），ResizeObserver 应把 scrollTop 重钉到 1500
      mockScrollMetrics(listEl, 1500)
      triggerResize(listEl)

      // 重新钉底到新的 scrollHeight，不会停在中间
      expect(listEl.scrollTop).toBe(1500)
    })

    it('向上加载更多（prepend）后不弹到底部（sequence 更小的历史消息追加）', () => {
      // 初始只有 1 条消息（sequence=10）
      const initialMessages = [makeMessage({ id: 'msg-10', sequence: 10 })]
      const { container, rerender } = render(
        <MessageList
          {...defaultProps}
          messages={initialMessages}
          tabId="prepend-anchor"
          hasMore={true}
          isLoadingMore={false}
        />,
      )
      const listEl = container.querySelector('[data-testid="message-list"]') as HTMLElement
      mockScrollMetrics(listEl, 1000)
      flushRaf()
      // 初始钉底
      expect(listEl.scrollTop).toBe(1000)

      // 用户上滑到中间
      listEl.scrollTop = 500

      // 触发加载更多：isLoadingMore 变 true
      rerender(
        <MessageList
          {...defaultProps}
          messages={initialMessages}
          tabId="prepend-anchor"
          hasMore={true}
          isLoadingMore={true}
        />,
      )

      // 加载完成：prepend 历史消息（sequence=5，更小），isLoadingMore 变 false
      const prependedMessages = [
        makeMessage({ id: 'msg-5', sequence: 5 }),
        ...initialMessages,
      ]
      // 内容高度增大（模拟老消息撑高了 400px）
      mockScrollMetrics(listEl, 1400)
      rerender(
        <MessageList
          {...defaultProps}
          messages={prependedMessages}
          tabId="prepend-anchor"
          hasMore={true}
          isLoadingMore={false}
        />,
      )
      flushRaf()

      // 关键断言：prepend 后没有因为 length 增加而弹到 scrollHeight（1400），
      // 也没有弹到旧的 1000。isFollowingBottom 被置为 false，钉底逻辑不触发。
      expect(listEl.scrollTop).toBe(500)
    })
  })
})
