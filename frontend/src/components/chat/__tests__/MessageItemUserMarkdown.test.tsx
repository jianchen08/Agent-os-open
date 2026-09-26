/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * 用户消息统一 markdown 渲染测试（ADR 2026-08-21）。
 *
 * 用户消息从纯文本（whitespace-pre-wrap）改为与 assistant 同款 markdown：
 * 附件索引随 content 携带（![f](/uploads/x.png)）由此直接渲染成图/链接，
 * 历史回读（内核只存 content）刷新不丢。
 */
import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

// LobeChatMarkdown 拉起 @lobehub/ui 全家桶——单测用轻量桩替身，
// 断言"用户 content 交给了 markdown 渲染器"即可（渲染细节归其自身测试）。
vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
vi.mock('@/stores/agentStore', async () => (await import('./helpers/messageItemMocks')).agentStoreEmptyMock())
vi.mock('@/stores/interactionStore', async () => (await import('./helpers/messageItemMocks')).interactionStoreEmptyMock())
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/attachmentOpener', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())
vi.mock('@/components/chat/MessageActions', async () => (await import('./helpers/messageItemMocks')).messageActionsStubMock())
vi.mock('@/components/chat/MessageContentRenderer', async () => (await import('./helpers/messageItemMocks')).messageContentRendererStubMock())
vi.mock('@/components/chat/hooks/useMessageRender', async () => (await import('./helpers/messageItemMocks')).useMessageRenderStubMock())

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
    renderWithProviders(<MessageItem message={makeUserMessage(content)} />)
    const md = screen.getByTestId('user-markdown')
    expect(md.textContent).toContain('看看这张')
    expect(md.textContent).toContain('![cat.png](/uploads/cat.png)')
  })

  it('纯文本用户消息同样走 markdown 渲染器（统一路径，无纯文本分支）', () => {
    renderWithProviders(<MessageItem message={makeUserMessage('纯文本')} />)
    expect(screen.getByTestId('user-markdown').textContent).toBe('纯文本')
  })
})
