// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * 消息流渲染窗口化契约测试（renderer 内存优化项 2）
 *
 * 长会话历史消息的 Blink 布局/绘制对象是 renderer 内存大户。每条消息
 * 包裹层必须标记渲染窗口化（content-visibility:auto + contain-intrinsic-size）：
 * 视口外的消息由浏览器跳过布局/绘制（等价"仅渲染可视窗口±缓冲"），DOM 保留
 * （滚动跟随/锚点恢复/定位跳转等滚动模型零改动）。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MessageList } from '../MessageList'
import type { ExtendedMessageListProps } from '../MessageList'
import type { Message } from '@/types/models'

vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: Message }) => (
    <div data-testid={`message-item-${message.id}`}>{message.content}</div>
  ),
}))

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'sess-1',
    sequence: 1,
    role: 'user',
    content: '内容',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

const baseProps: ExtendedMessageListProps = {
  messages: [],
  className: '',
}

describe('消息流渲染窗口化', () => {
  it('每条消息包裹层携带渲染窗口化标记（content-visibility 类）', () => {
    const messages = [
      makeMessage({ id: 'm1', sequence: 1 }),
      makeMessage({ id: 'm2', sequence: 2, role: 'assistant' }),
      makeMessage({ id: 'm3', sequence: 3 }),
    ]
    const { container } = render(<MessageList {...baseProps} messages={messages} />)

    const wrappers = container.querySelectorAll('[data-msg-id]')
    expect(wrappers).toHaveLength(3)
    for (const w of wrappers) {
      expect(w.className).toContain('message-render-window')
    }
  })

  it('渲染窗口化不影响消息内容渲染（DOM 保留）', () => {
    const messages = [
      makeMessage({ id: 'm1', sequence: 1, content: '第一条' }),
      makeMessage({ id: 'm2', sequence: 2, content: '第二条', role: 'assistant' }),
    ]
    render(<MessageList {...baseProps} messages={messages} />)
    expect(screen.getByTestId('message-item-m1')).toBeInTheDocument()
    expect(screen.getByTestId('message-item-m2')).toBeInTheDocument()
  })
})
