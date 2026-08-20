/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * 用户消息统一 markdown 渲染测试（ADR 2026-08-21）。
 *
 * 用户消息从纯文本（whitespace-pre-wrap）改为与 assistant 同款 markdown：
 * 附件索引随 content 携带（![f](/uploads/x.png)）由此直接渲染成图/链接，
 * 历史回读（内核只存 content）刷新不丢。
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

// LobeChatMarkdown 拉起 @lobehub/ui 全家桶——单测用轻量桩替身，
// 断言"用户 content 交给了 markdown 渲染器"即可（渲染细节归其自身测试）。
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
  useInteractionStore: () => ({ pendingInteractions: [] }),
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

function makeUserMessage(content: string): Message {
  return {
    id: 'user-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'user',
    content,
    timestamp: new Date().toISOString(),
    status: 'completed',
  } as Message
}

describe('用户消息 markdown 统一渲染', () => {
  afterEach(() => vi.clearAllMocks())

  it('用户 content 经 LobeChatMarkdown 渲染（含附件引用原文）', () => {
    const content = '看看这张\n\n![cat.png](/uploads/cat.png)'
    render(<MessageItem message={makeUserMessage(content)} />)
    const md = screen.getByTestId('user-markdown')
    expect(md.textContent).toContain('看看这张')
    expect(md.textContent).toContain('![cat.png](/uploads/cat.png)')
  })

  it('纯文本用户消息同样走 markdown 渲染器（统一路径，无纯文本分支）', () => {
    render(<MessageItem message={makeUserMessage('纯文本')} />)
    expect(screen.getByTestId('user-markdown').textContent).toBe('纯文本')
  })
})
