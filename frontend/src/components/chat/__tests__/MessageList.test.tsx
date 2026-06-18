/**
 * MessageList 组件测试
 *
 * 验证消息列表的渲染逻辑：
 * - 空消息列表显示占位符
 * - 有消息时正确渲染消息项
 * - isGenerating 状态下显示思考中提示
 * - 传入不同 props 的渲染行为
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import type { Message } from '@/types/models'

// Mock Virtuoso（避免浏览器环境依赖）
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data: Message[]; itemContent: (index: number) => React.ReactNode }) => (
    <div data-testid="virtuoso-mock">
      {data.map((_: unknown, i: number) => (
        <div key={i} data-testid={`virtuoso-item-${i}`}>{itemContent(i)}</div>
      ))}
    </div>
  ),
}))

// Mock useMessageScroll hook
vi.mock('../hooks/useMessageScroll', () => ({
  useMessageScroll: () => ({
    virtuosoRef: { current: null },
    containerRef: { current: null },
    shouldFollowOutput: true,
    onScroll: vi.fn(),
    handleStartReached: vi.fn(),
    HeaderComponent: () => null,
    initialTopMostItemIndex: 0,
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

import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'

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

    it('渲染 Virtuoso 虚拟列表', () => {
      const messages = [makeMessage({ id: 'msg-1' })]
      render(<MessageList {...defaultProps} messages={messages} />)
      expect(screen.getByTestId('virtuoso-mock')).toBeInTheDocument()
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
})
