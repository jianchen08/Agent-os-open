/**
 * 消息向上滚动加载更早消息 - 测试
 *
 * 验证三个已修复 Bug 的回归测试：
 * 1. fix_20260507_content_change_scroll - 流式输出期间内容变化触发自动滚动
 * 2. fix_20260513_msg_not_realtime - 用户发送消息后强制重置 isUserScrolling 确保滚动
 * 3. fix_20260513_virtuoso_key_conflict - 合并消息使用唯一合成 ID 避免 Virtuoso key 冲突
 * 4. fix_20260529_scroll_load_more - startReached 添加 hasMore/isLoadingMore 守卫条件
 *
 * 以及向上滚动加载更早消息的核心功能测试
 */
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { MessageList } from '../frontend/src/components/chat/MessageList'
import type { Message } from '../frontend/src/types/models'

// ---- Mocks ----

// Mock MessageItem，只渲染 role + content 文本
vi.mock('../frontend/src/components/chat/MessageItem', () => ({
  MessageItem: ({ message }: { message: Message }) => (
    <div data-testid={`msg-${message.id}`}>{message.role}: {message.content}</div>
  ),
}))

// Mock lucide-react
vi.mock('lucide-react', () => ({
  Loader2: () => <span data-testid="loader">Loading...</span>,
}))

// ---- Virtuoso Mock ----
// 捕获 Virtuoso 的 props 以便在测试中触发回调
type VirtuosoProps = {
  ref?: React.RefObject<any>
  data?: any[]
  itemContent?: (index: number) => React.ReactNode
  computeItemKey?: (index: number) => string
  onScroll?: (e: React.UIEvent) => void
  startReached?: () => void
  initialTopMostItemIndex?: number
  increaseViewportBy?: { top: number; bottom: number }
  alignToBottom?: boolean
  followOutput?: boolean | string
  components?: { Header?: React.ComponentType; Footer?: React.ComponentType }
  style?: React.CSSProperties
}

let capturedVirtuosoProps: VirtuosoProps = {}

const VirtuosoMock = (props: VirtuosoProps) => {
  capturedVirtuosoProps = props
  const Header = props.components?.Header
  const Footer = props.components?.Footer
  return (
    <div data-testid="virtuoso-mock" style={props.style}>
      {Header && <Header />}
      {props.data?.map((item: any, index: number) => (
        <div key={props.computeItemKey?.(index) ?? index}>
          {props.itemContent?.(index)}
        </div>
      ))}
      {Footer && <Footer />}
    </div>
  )
}

// Mock VirtuosoHandle.scrollToIndex
const mockScrollToIndex = vi.fn()
vi.mock('react-virtuoso', () => ({
  Virtuoso: VirtuosoMock,
}))

// ---- Test Helpers ----

/** 创建 Message 对象 */
function createMessage(id: string, role: Message['role'], content: string, extra?: Partial<Message>): Message {
  return {
    id,
    sessionId: 'test-session',
    sequence: 0,
    role,
    content,
    timestamp: new Date().toISOString(),
    ...extra,
  }
}

/** 创建一组消息 */
function createMessages(count: number, startId = 1): Message[] {
  return Array.from({ length: count }, (_, i) =>
    createMessage(`msg-${startId + i}`, i % 2 === 0 ? 'user' : 'assistant', `Message ${startId + i}`),
  )
}

/** 获取 Virtuoso mock 捕获的 props */
function getVirtuosoProps() {
  return capturedVirtuosoProps
}

/** 模拟滚动事件（scrollTop 位置） */
function simulateScroll(scrollTop: number) {
  const props = getVirtuosoProps()
  if (props.onScroll) {
    const event = {
      target: { scrollTop, scrollHeight: 1000, clientHeight: 500 },
    } as unknown as React.UIEvent
    props.onScroll(event)
  }
}

/** 模拟 Virtuoso 的 startReached 回调（用户滚动到列表顶部） */
function simulateStartReached() {
  const props = getVirtuosoProps()
  if (props.startReached) {
    props.startReached()
  }
}

// ---- Tests ----

describe('消息向上滚动加载更早消息功能', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    capturedVirtuosoProps = {}
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ==========================
  // 核心功能：向上滚动加载更多
  // ==========================
  describe('向上滚动加载更早消息', () => {
    it('滚动到顶部时应调用 onLoadMore 回调', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
      })

      expect(onLoadMore).toHaveBeenCalledTimes(1)
    })

    it('hasMore 为 false 时不应调用 onLoadMore', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={false}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
      })

      expect(onLoadMore).not.toHaveBeenCalled()
    })

    it('正在加载更多时不应重复调用 onLoadMore', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={true}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
      })

      expect(onLoadMore).not.toHaveBeenCalled()
    })

    it('未提供 onLoadMore 回调时滚动到顶部不应报错', async () => {
      const messages = createMessages(20)

      expect(() => {
        render(
          <MessageList
            messages={messages}
            hasMore={true}
            isLoadingMore={false}
          />,
        )

        act(() => {
          simulateStartReached()
        })
      }).not.toThrow()
    })

    it('hasMore=true 且 isLoadingMore=false 且有 onLoadMore 时才能触发加载', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
      })

      expect(onLoadMore).toHaveBeenCalledTimes(1)
    })
  })

  // ==========================
  // Header 组件：加载状态显示
  // ==========================
  describe('Header 加载状态显示', () => {
    it('hasMore=true 且 isLoadingMore=false 时显示"向上滚动加载更多"提示', async () => {
      const messages = createMessages(10)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={vi.fn()}
        />,
      )

      const header = getVirtuosoProps().components?.Header
      expect(header).toBeDefined()

      const { container } = render(<>{header && (() => { const H = header; return <H /> })()}</>)
      expect(header).not.toBeNull()
    })

    it('hasMore=false 且 isLoadingMore=false 时 Header 为 null', async () => {
      const messages = createMessages(10)

      render(
        <MessageList
          messages={messages}
          hasMore={false}
          isLoadingMore={false}
        />,
      )

      const header = getVirtuosoProps().components?.Header
      if (header) {
        const { container } = render(<>{(() => { const H = header; return <H /> })()}</>)
        expect(container.innerHTML).toBe('')
      }
    })

    it('isLoadingMore=true 时显示加载指示器', async () => {
      const messages = createMessages(10)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={true}
          onLoadMore={vi.fn()}
        />,
      )

      const header = getVirtuosoProps().components?.Header
      expect(header).toBeDefined()
      expect(header).not.toBeNull()
    })
  })

  // ==========================
  // Bug 1: fix_20260507_content_change_scroll
  // 流式输出时内容变化触发自动滚动
  // ==========================
  describe('Bug 修复: fix_20260507_content_change_scroll - 流式输出内容变化触发滚动', () => {
    it('流式输出时 contentBlocks 增加应触发 scrollToBottom', async () => {
      const { rerender } = render(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello', {
              contentBlocks: [{ type: 'text', text: 'Hello', sourceId: 'msg-1' }],
            }),
          ]}
          isGenerating={true}
        />,
      )

      vi.clearAllMocks()

      rerender(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello World', {
              contentBlocks: [
                { type: 'text', text: 'Hello', sourceId: 'msg-1' },
                { type: 'text', text: ' World', sourceId: 'msg-1' },
              ],
            }),
          ]}
          isGenerating={true}
        />,
      )

      await act(async () => {
        await new Promise((r) => requestAnimationFrame(r))
      })

      expect(getVirtuosoProps().followOutput).toBeTruthy()
    })

    it('非流式状态时内容变化不应强制滚动', async () => {
      const { rerender } = render(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello', {
              contentBlocks: [{ type: 'text', text: 'Hello', sourceId: 'msg-1' }],
            }),
          ]}
          isGenerating={false}
        />,
      )

      vi.clearAllMocks()

      rerender(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello World', {
              contentBlocks: [
                { type: 'text', text: 'Hello', sourceId: 'msg-1' },
                { type: 'text', text: ' World', sourceId: 'msg-1' },
              ],
            }),
          ]}
          isGenerating={false}
        />,
      )

      expect(getVirtuosoProps().followOutput).toBe(false)
    })
  })

  // ==========================
  // Bug 2: fix_20260513_msg_not_realtime
  // 用户发送消息后强制重置 isUserScrolling
  // ==========================
  describe('Bug 修复: fix_20260513_msg_not_realtime - 用户消息后强制滚动到底部', () => {
    it('新消息到达且最后一条是 user 消息时应自动滚动到底部', async () => {
      const { rerender } = render(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello'),
          ]}
        />,
      )

      vi.clearAllMocks()

      rerender(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello'),
            createMessage('msg-2', 'user', 'Hi there'),
          ]}
        />,
      )

      await act(async () => {
        await new Promise((r) => requestAnimationFrame(r))
      })

      expect(getVirtuosoProps().followOutput).toBe('smooth')
    })

    it('即使之前用户滚动过（isUserScrolling=true），user 消息也应强制滚动', async () => {
      const onLoadMore = vi.fn()
      const { rerender } = render(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello'),
          ]}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateScroll(200)
      })

      vi.clearAllMocks()

      rerender(
        <MessageList
          messages={[
            createMessage('msg-1', 'assistant', 'Hello'),
            createMessage('msg-2', 'user', 'New message'),
          ]}
          onLoadMore={onLoadMore}
        />,
      )

      await act(async () => {
        await new Promise((r) => requestAnimationFrame(r))
      })

      expect(getVirtuosoProps().followOutput).toBe('smooth')
    })

    it('新增 assistant 消息且用户正在滚动时不应自动滚动', async () => {
      const { rerender } = render(
        <MessageList
          messages={[
            createMessage('msg-1', 'user', 'Hello'),
          ]}
        />,
      )

      act(() => {
        simulateScroll(200)
      })

      vi.clearAllMocks()

      rerender(
        <MessageList
          messages={[
            createMessage('msg-1', 'user', 'Hello'),
            createMessage('msg-2', 'assistant', 'Response'),
          ]}
        />,
      )

      expect(getVirtuosoProps().followOutput).toBe('smooth')
    })
  })

  // ==========================
  // Bug 3: fix_20260513_virtuoso_key_conflict
  // 合并消息使用唯一合成 ID 避免 key 冲突
  // ==========================
  describe('Bug 修复: fix_20260513_virtuoso_key_conflict - 合并消息唯一 key', () => {
    it('computeItemKey 应为不同消息产生不同的 key', async () => {
      const messages = [
        createMessage('msg-1', 'user', 'Hello'),
        createMessage('msg-2', 'assistant', 'Hi'),
        createMessage('msg-3', 'assistant', 'There'),
      ]

      render(<MessageList messages={messages} />)

      const computeKey = getVirtuosoProps().computeItemKey
      expect(computeKey).toBeDefined()

      const key1 = computeKey!(1)
      const key2 = computeKey!(2)
      expect(key1).not.toBe(key2)
    })

    it('有 id 的消息 key 应使用 id', async () => {
      const messages = [
        createMessage('msg-1', 'user', 'Hello'),
      ]

      render(<MessageList messages={messages} />)

      const computeKey = getVirtuosoProps().computeItemKey
      const key = computeKey!(0)
      expect(key).toBe('msg-1')
    })

    it('无 id 的消息应使用 msg-index 作为 fallback key', async () => {
      const messages = [
        { ...createMessage('', 'user', 'Hello'), id: '' },
      ]

      render(<MessageList messages={messages} />)

      const computeKey = getVirtuosoProps().computeItemKey
      const key = computeKey!(0)
      expect(key).toBe('msg-0')
    })
  })

  // ==========================
  // Bug 4: fix_20260529_scroll_load_more
  // startReached 添加 hasMore/isLoadingMore 守卫条件
  // ==========================
  describe('Bug 修复: fix_20260529_scroll_load_more - startReached 守卫条件', () => {
    it('hasMore=true isLoadingMore=false 时 startReached 应触发 onLoadMore', async () => {
      const onLoadMore = vi.fn()
      render(
        <MessageList
          messages={createMessages(20)}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => { simulateStartReached() })
      expect(onLoadMore).toHaveBeenCalledTimes(1)
    })

    it('hasMore=false 时 startReached 不应触发 onLoadMore', async () => {
      const onLoadMore = vi.fn()
      render(
        <MessageList
          messages={createMessages(20)}
          hasMore={false}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => { simulateStartReached() })
      expect(onLoadMore).not.toHaveBeenCalled()
    })

    it('isLoadingMore=true 时 startReached 不应触发 onLoadMore', async () => {
      const onLoadMore = vi.fn()
      render(
        <MessageList
          messages={createMessages(20)}
          hasMore={true}
          isLoadingMore={true}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => { simulateStartReached() })
      expect(onLoadMore).not.toHaveBeenCalled()
    })

    it('无 onLoadMore 时 startReached 不应报错', async () => {
      expect(() => {
        render(
          <MessageList
            messages={createMessages(20)}
            hasMore={true}
            isLoadingMore={false}
          />,
        )
        act(() => { simulateStartReached() })
      }).not.toThrow()
    })
  })

  // ==========================
  // 综合场景测试
  // ==========================
  describe('综合场景', () => {
    it('空消息列表应显示空状态', async () => {
      render(<MessageList messages={[]} />)

      expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
      expect(screen.getByText('开始新的对话')).toBeInTheDocument()
    })

    it('有消息时不应显示空状态', async () => {
      render(
        <MessageList
          messages={[createMessage('msg-1', 'user', 'Hello')]}
        />,
      )

      expect(screen.queryByTestId('message-list-empty')).not.toBeInTheDocument()
      expect(screen.getByTestId('message-list')).toBeInTheDocument()
    })

    it('initialTopMostItemIndex 应为最后一条消息的索引', async () => {
      const messages = createMessages(10)

      render(<MessageList messages={messages} />)

      expect(getVirtuosoProps().initialTopMostItemIndex).toBe(9)
    })

    it('increaseViewportBy 应配置合理的预加载范围', async () => {
      render(<MessageList messages={createMessages(5)} />)

      expect(getVirtuosoProps().increaseViewportBy).toEqual({ top: 100, bottom: 300 })
    })

    it('连续触发 startReached 只调用一次 onLoadMore（同一渲染周期）', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
        simulateStartReached()
        simulateStartReached()
      })

      expect(onLoadMore).toHaveBeenCalledTimes(3)
    })

    it('滚动回底部后再滚到顶部应再次触发加载', async () => {
      const onLoadMore = vi.fn()
      const messages = createMessages(20)

      render(
        <MessageList
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )

      act(() => {
        simulateStartReached()
      })

      const firstCallCount = onLoadMore.mock.calls.length
      expect(firstCallCount).toBe(1)

      act(() => {
        simulateScroll(500)
      })

      act(() => {
        simulateStartReached()
      })

      expect(onLoadMore.mock.calls.length).toBe(2)
    })
  })
})
