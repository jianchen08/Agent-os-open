// @feature: FP-0.2.流式活性统一 | @ci: frontend-test
/**
 * MessageList 等待 AI 响应壳测试（ADR 2026-11-17-awaiting-reply-render-shell）：
 * - awaitingReplySince > 0 且尾消息为本次发送的 user 消息 → 思考中壳上屏
 *   （三阶段缝隙窗口的 AI 侧即时反馈）
 * - 尾消息为 assistant（真气泡已在场）→ 不渲染壳
 * - 尾消息 user 但 failed/error（发送失败）→ 不渲染壳
 * - awaitingReplySince = 0（流式中/排队中，上游 ChatContainer 已归零）→ 不渲染壳
 *
 * MessageItem mock 掉（同 MessageList.test.tsx 惯例，避免深入渲染依赖）。
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageList } from '../MessageList'
import type { Message } from '@/types/models'

vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: Message }) => (
    <div data-testid={`message-item-${message.id}`}>
      <span>{message.content}</span>
    </div>
  ),
}))

// 段清单是 MessageList 挂载旁路面（activePipelineId 在场即拉取），桩为空清单
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))
vi.mock('@/utils/logger', async () =>
  (await import('@/stores/__tests__/helpers/storeTestMocks')).loggerMockFull(),
)

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

const baseProps = { isGenerating: false, className: '' } as const

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MessageList 等待 AI 响应壳', () => {
  it('awaitingReplySince > 0 且尾消息 user → 壳上屏', () => {
    render(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'u1', role: 'user', content: '你好', status: 'completed' })]}
        awaitingReplySince={Date.now()}
      />,
    )
    expect(screen.getByTestId('awaiting-reply-shell')).toBeInTheDocument()
    // 2026-11-19 用户裁定：全站活动态文案统一「执行中」（壳内 StreamActivityIndicator 同源）
    expect(screen.getByText('执行中')).toBeInTheDocument()
  })

  it('尾消息 assistant（真气泡已在场）→ 不渲染壳', () => {
    render(
      <MessageList
        {...baseProps}
        messages={[
          makeMessage({ id: 'u1', role: 'user', status: 'completed' }),
          makeMessage({ id: 'a1', role: 'assistant', status: 'streaming' }),
        ]}
        awaitingReplySince={Date.now()}
      />,
    )
    expect(screen.queryByTestId('awaiting-reply-shell')).not.toBeInTheDocument()
  })

  it('尾消息 user 但 failed（发送失败）→ 不渲染壳', () => {
    render(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'u1', role: 'user', status: 'failed' })]}
        awaitingReplySince={Date.now()}
      />,
    )
    expect(screen.queryByTestId('awaiting-reply-shell')).not.toBeInTheDocument()
  })

  it('awaitingReplySince = 0（流式中/排队中归零）→ 不渲染壳', () => {
    render(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'u1', role: 'user', status: 'completed' })]}
        awaitingReplySince={0}
      />,
    )
    expect(screen.queryByTestId('awaiting-reply-shell')).not.toBeInTheDocument()
  })

  it('壳缺省不传 → 不渲染壳（既有调用方零影响）', () => {
    render(
      <MessageList
        {...baseProps}
        messages={[makeMessage({ id: 'u1', role: 'user', status: 'completed' })]}
      />,
    )
    expect(screen.queryByTestId('awaiting-reply-shell')).not.toBeInTheDocument()
  })
})
