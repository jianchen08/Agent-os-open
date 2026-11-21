// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * 长会话（5000 条）消息列表渲染测试（内存封顶 20000 + 渲染窗口化配套）
 *
 * 数据层封顶提到 20000 后（原 2000 触发即关闸已废除），单管道内存桶可容纳
 * 5000+ 条消息——本测试证明该规模下 MessageList 可完整渲染、消息不丢不重、
 * 每条消息携带渲染窗口化标记（content-visibility：视口外跳过布局/绘制，
 * Blink 侧渲染负载与可视窗口成正比而非总条数），并留痕挂载/追加重渲耗时
 * （jsdom 数字仅供横向对比，真机滚动流畅度由 content-visibility 承担）。
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

function makeMessage(seq: number): Message {
  return {
    id: `m-${seq}`,
    sessionId: 'sess-long',
    sequence: seq,
    role: seq % 2 ? 'user' : 'assistant',
    content: `消息 ${seq}`,
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    status: 'completed',
  } as Message
}

const baseProps: ExtendedMessageListProps = {
  messages: [],
  className: '',
}

describe('MessageList 长会话（5000 条）', () => {
  it('5000 条完整渲染：不丢不重、全量携带渲染窗口化标记，追加重渲可承受', () => {
    const messages = Array.from({ length: 5000 }, (_, i) => makeMessage(i + 1))
    const t0 = performance.now()
    const { container, rerender } = render(<MessageList {...baseProps} messages={messages} />)
    const mountMs = performance.now() - t0

    // 不丢不重：5000 个节点全部在 DOM（merge 过滤/吸收不得吞掉独立 user/assistant 轮次）
    const wrappers = container.querySelectorAll('[data-msg-id]')
    expect(wrappers).toHaveLength(5000)
    const ids = new Set(Array.from(wrappers).map((w) => w.getAttribute('data-msg-id')))
    expect(ids.size).toBe(5000)
    // 渲染窗口化标记逐条携带（视口外消息跳过 Blink 布局/绘制的载体）
    for (const w of wrappers) {
      expect(w.className).toContain('message-render-window')
    }

    // 追加一条新消息（模拟长会话持续对话的单次重渲负载）
    const t1 = performance.now()
    rerender(<MessageList {...baseProps} messages={[...messages, makeMessage(5001)]} />)
    const rerenderMs = performance.now() - t1
    expect(container.querySelectorAll('[data-msg-id]')).toHaveLength(5001)
    expect(screen.getByTestId('message-item-m-5001')).toBeInTheDocument()

    // 耗时留痕（jsdom 无布局引擎，数字供横向对比；不设紧阈值防 CI 抖动）
     
    console.info(
      `[MessageList.longSession] 5000 条 mount=${mountMs.toFixed(0)}ms, 追加1条重渲=${rerenderMs.toFixed(0)}ms`,
    )
    // 宽上界防退化（jsdom 正常值数百 ms 级；超 30s 说明渲染复杂度失控）
    expect(mountMs).toBeLessThan(30_000)
    expect(rerenderMs).toBeLessThan(30_000)
  })
})
