/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * 失败消息渲染测试（2026-08-22 错误透传收口）。
 *
 * status=error 且无内容的 assistant 消息不得走"空内容隐藏"逻辑整块消失
 * （用户会看到消息凭空消失）；失败/中断消息强制渲染错误态文案。
 */
import { screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
vi.mock('@/stores/agentStore', async () => (await import('./helpers/messageItemMocks')).agentStoreEmptyMock())
vi.mock('@/stores/interactionStore', async () => (await import('./helpers/messageItemMocks')).interactionStoreSelectorMock())
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/attachmentOpener', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())
vi.mock('@/components/chat/MessageActions', async () => (await import('./helpers/messageItemMocks')).messageActionsStubMock())
vi.mock('@/components/chat/MessageContentRenderer', async () => (await import('./helpers/messageItemMocks')).messageContentRendererStubMock())
vi.mock('@/components/chat/hooks/useMessageRender', async () => (await import('./helpers/messageItemMocks')).useMessageRenderStubMock())

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
