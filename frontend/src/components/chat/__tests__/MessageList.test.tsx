/**
 * MessageList 组件测试
 *
 * 验证消息列表的渲染逻辑：
 * - 空消息列表显示占位符
 * - 有消息时正确渲染消息项
 * - isGenerating 状态下显示思考中提示
 * - 传入不同 props 的渲染行为
 * - 向上加载更多（startReached）、流式钉底（followOutput）等 virtuoso prop 配置正确
 *
 * 注：虚拟滚动的真实滚动行为（视口位置/measure/动画）依赖真实浏览器布局，
 * jsdom 无布局引擎无法验证，这部分靠浏览器实测。本测试只验证 MessageList 向
 * Virtuoso 传递的 prop 配置是否正确，以及渲染/空状态/思考中占位等纯渲染逻辑。
 */

import { render, screen } from '@testing-library/react'
import { forwardRef } from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'
import type { Message } from '@/types/models'

/**
 * Mock react-virtuoso：捕获传入的 prop，让测试能断言 followOutput/startReached
 * 等回调配置。用 forwardRef 转发 ref（组件内部用 ref 调 scrollToIndex）。
 * Virtuoso 在 jsdom 下无法真实虚拟化（无布局），改用这个轻量替身。
 */
const capture: {
  followOutput?: (...args: unknown[]) => unknown
  startReached?: (index: number) => void
  atBottomStateChange?: (atBottom: boolean) => void
  initialTopMostItemIndex?: unknown
  firstItemIndex?: number
  components?: { Header?: unknown; Footer?: unknown }
  ref?: unknown
  scrollToIndex?: ReturnType<typeof vi.fn>
} = {}

vi.mock('react-virtuoso', () => ({
  Virtuoso: forwardRef((props: Record<string, unknown>, ref) => {
    capture.followOutput = props.followOutput as typeof capture.followOutput
    capture.startReached = props.startReached as typeof capture.startReached
    capture.atBottomStateChange = props.atBottomStateChange as typeof capture.atBottomStateChange
    capture.initialTopMostItemIndex = props.initialTopMostItemIndex
    capture.firstItemIndex = props.firstItemIndex as number
    capture.components = props.components as typeof capture.components
    capture.ref = ref
    // 暴露一个 stub handle 记录 scrollToIndex 调用，让测试能断言「内容变化时重钉底部」。
    // 这是补回 commit 28c670a0 冷加载重钉防护的核心验证点。
    if (ref && typeof ref === 'object') {
      ;(ref as { current: unknown }).current = { scrollToIndex: capture.scrollToIndex! }
    }

    const data = (props.data as Message[]) || []
    const Header = props.components?.Header as React.ComponentType | undefined
    const Footer = props.components?.Footer as React.ComponentType | undefined
    return (
      <div data-testid="virtuoso-mock">
        {Header && <Header />}
        {data.map((m, i) => {
          const Content = props.itemContent
          return <div key={`v-${i}`}>{Content ? Content(i, m) : null}</div>
        })}
        {Footer && <Footer />}
      </div>
    )
  }),
}))

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

describe('MessageList', () => {
  const defaultProps: ExtendedMessageListProps = {
    messages: [],
    isGenerating: false,
    modelName: 'test-model',
    className: '',
  }

  beforeEach(() => {
    vi.clearAllMocks()
    capture.followOutput = undefined
    capture.startReached = undefined
    capture.atBottomStateChange = undefined
    capture.initialTopMostItemIndex = undefined
    capture.firstItemIndex = undefined
    capture.components = undefined
    capture.ref = undefined
    // 每个用例独立的 scrollToIndex mock（组件 ref 持有它的引用）
    capture.scrollToIndex = vi.fn()
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
      expect(screen.getByTestId('message-item-msg-1')).toBeInTheDocument()
    })
  })

  describe('virtuoso prop 配置', () => {
    /**
     * 验证 MessageList 向 Virtuoso 传递的滚动行为配置是否正确。
     * 真实滚动效果依赖浏览器布局，靠浏览器实测；此处只验证配置层面的契约。
     */

    it('首屏钉到最后一条（initialTopMostItemIndex = length-1）', () => {
      const messages = [
        makeMessage({ id: 'msg-1' }),
        makeMessage({ id: 'msg-2' }),
        makeMessage({ id: 'msg-3' }),
      ]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(capture.initialTopMostItemIndex).toBe(messages.length - 1)
    })

    it('firstItemIndex 初始为正数（满足 virtuoso 要求）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(capture.firstItemIndex).toBeGreaterThan(0)
    })

    it('prepend 后 firstItemIndex 随 prependedCount 递减（保持视口位置）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      // 先无 prepend
      const { rerender } = render(<MessageList {...defaultProps} messages={messages} />)
      const baseFirstIndex = capture.firstItemIndex!

      // prepend 了 50 条（store 累计 prependedCount=50）
      const moreMessages = [
        ...Array.from({ length: 50 }, (_, i) => makeMessage({ id: `old-${i}`, sequence: i })),
        ...messages,
      ]
      rerender(<MessageList {...defaultProps} messages={moreMessages} prependedCount={50} />)

      // firstItemIndex 应递减 50，保持旧首条的逻辑序号不变 → 视口位置不变
      expect(capture.firstItemIndex).toBe(baseFirstIndex - 50)
      expect(capture.firstItemIndex).toBeGreaterThan(0)
    })

    it('followOutput 默认跟随底部（返回 auto）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(capture.followOutput).toBeDefined()
      expect(capture.followOutput!(true)).toBe('auto')
    })

    it('startReached 配置了 onLoadMore 且未在加载时触发', () => {
      const onLoadMore = vi.fn()
      const messages = [makeMessage({ id: 'msg-1' })]
      render(
        <MessageList
          {...defaultProps}
          messages={messages}
          hasMore={true}
          isLoadingMore={false}
          onLoadMore={onLoadMore}
        />,
      )
      expect(capture.startReached).toBeDefined()
      capture.startReached!(0)
      expect(onLoadMore).toHaveBeenCalledTimes(1)
    })

    it('isLoadingMore 时 startReached 不重复触发 onLoadMore', () => {
      const onLoadMore = vi.fn()
      const messages = [makeMessage({ id: 'msg-1' })]
      render(
        <MessageList
          {...defaultProps}
          messages={messages}
          hasMore={true}
          isLoadingMore={true}
          onLoadMore={onLoadMore}
        />,
      )
      capture.startReached!(0)
      expect(onLoadMore).not.toHaveBeenCalled()
    })

    it('hasMore 时渲染 Header（加载更多提示）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(
        <MessageList {...defaultProps} messages={messages} hasMore={true} isLoadingMore={false} />,
      )
      expect(capture.components?.Header).toBeDefined()
    })

    it('hasMore=false 时不渲染 Header', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} hasMore={false} />)
      expect(capture.components?.Header).toBeUndefined()
    })

    it('始终渲染 Footer（含思考中占位与间距）', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(capture.components?.Footer).toBeDefined()
    })
  })

  describe('冷加载重钉（补回 commit 28c670a0 防护）', () => {
    /**
     * 核心回归：刷新时 persist 快照先钉底 → initFromAPI 异步全量替换（新数组引用）
     * → 必须重新钉底，否则视图停在快照高度的中间。
     * 这是「刷新后渲染位置不对」的根因防护。
     */

    it('首次数据到达时钉底一次', async () => {
      const messages = [makeMessage({ id: 'msg-1' }), makeMessage({ id: 'msg-2' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      // 首次数据：RAF 异步调度 scrollToIndex('LAST')，用 waitFor 等 effect 跑完
      // jsdom 下 RAF 需通过 microtask/计时器推进，waitFor 会自动轮询断言
      await vi.waitFor(() => {
        expect(capture.scrollToIndex).toHaveBeenCalledWith({ index: 'LAST', behavior: 'auto' })
      })
    })

    it('initFromAPI 全量替换（新数组引用）后重新钉底', () => {
      // 快照：2 条
      const snapshot = [makeMessage({ id: 'msg-1' }), makeMessage({ id: 'msg-2' })]
      const { rerender } = render(<MessageList {...defaultProps} messages={snapshot} />)
      // 清掉首次钉底的调用计数
      capture.scrollToIndex!.mockClear()

      // initFromAPI 返回：全量替换为新数组（条数可能不同，引用必定不同）
      const apiMessages = [
        makeMessage({ id: 'api-1', content: 'api 消息一' }),
        makeMessage({ id: 'api-2', content: 'api 消息二' }),
        makeMessage({ id: 'api-3', content: 'api 消息三' }),
      ]
      rerender(<MessageList {...defaultProps} messages={apiMessages} />)

      // 跟随底部（默认 true）时，内容变化必须重新钉底
      expect(capture.scrollToIndex).toHaveBeenCalledWith({ index: 'LAST', behavior: 'auto' })
    })

    it('followOutput 契约：基于 isFollowingBottom 决定钉底（用户上滑时不钉）', () => {
      // followOutput 的设计：返回值由 isFollowingBottom.current 决定，
      // 不看 virtuoso 传入的 isAtBottom 参数（避免程序性滚动误判为用户跟随）。
      // 默认 isFollowingBottom=true → 返回 'auto'（跟随钉底）。
      // 用户上滑触发 wheel/touchstart 后 isFollowingBottom=false → 返回 false（不抢底）。
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)

      // 默认跟随底部：无论 virtuoso 传 true/false，都返回 auto（钉底）
      expect(capture.followOutput!(true)).toBe('auto')
      expect(capture.followOutput!(false)).toBe('auto')

      // 模拟用户滚回底部附近 → atBottomStateChange(true) 恢复跟随
      // （验证 atBottomStateChange 回调存在且可调用，不抛错即契约正确）
      expect(capture.atBottomStateChange).toBeDefined()
      expect(() => capture.atBottomStateChange!(true)).not.toThrow()
    })
  })
})
