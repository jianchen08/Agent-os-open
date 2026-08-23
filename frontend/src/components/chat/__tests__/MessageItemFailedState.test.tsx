/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * 失败消息渲染测试（2026-08-22 错误透传收口）。
 *
 * stream_error 标记 status=error 且无内容的 assistant 消息此前走
 * "空内容隐藏"逻辑整块消失——用户看到消息凭空消失；
 * 本次失败/中断消息强制渲染错误态文案。
 */
import { render, screen } from '@testing-library/react'
import { renderWithProviders } from '@/test/renderWithProviders'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="user-markdown">{content}</div>
  ),
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))
vi.mock('@/stores/agentStore', () => ({ useAgentStore: () => ({ agents: [] }) }))
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (selector: (s: { pendingInteractions: unknown[] }) => unknown) =>
    selector({ pendingInteractions: [] }),
}))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))
vi.mock('@/services/attachmentOpener', () => ({ openAttachment: vi.fn() }))
vi.mock('@/components/chat/MessageActions', () => ({
  MessageActions: () => null,
}))
vi.mock('@/components/chat/MessageContentRenderer', () => ({
  default: () => null,
}))
vi.mock('@/components/chat/hooks/useMessageRender', () => {
  const useMessageRender = () => ({ fragments: [], isStreaming: false })
  return { useMessageRender, default: useMessageRender }
})

function makeAssistantMessage(status: Message['status']): Message {
  return {
    id: 'assistant-1',
    sessionId: 'session-1',
    sequence: 2,
    role: 'assistant',
    content: '',
    parts: [],
    timestamp: new Date().toISOString(),
    status,
  } as Message
}

describe('失败/中断消息渲染（2026-08-22）', () => {
  afterEach(() => vi.clearAllMocks())

  it('error 状态空内容消息不消失，渲染失败文案', () => {
    renderWithProviders(<MessageItem message={makeAssistantMessage('error')} />)
    expect(screen.getByText('生成失败，请重试')).toBeDefined()
  })

  it('failed 状态空内容消息渲染中断文案', () => {
    renderWithProviders(<MessageItem message={makeAssistantMessage('failed')} />)
    expect(screen.getByText('生成已中断')).toBeDefined()
  })

  it('completed 状态空内容消息仍隐藏（不受影响）', () => {
    const { container } = renderWithProviders(<MessageItem message={makeAssistantMessage('completed')} />)
    expect(container.textContent).not.toContain('生成失败')
  })
})
