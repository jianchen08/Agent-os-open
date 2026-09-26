/** @feature FP-0.2.四 前端Schema @vision V6 可即用 @ci frontend-test */
/**
 * 引用消息渲染源无关测试（ADR 2026-09-10-generic-reference-protocol）。
 *
 * <reference source="..."> 块按解析出的 source 渲染「{source} 引用」行 +
 * {source}-node 卡片——非 godot 源零特判；godot 历史消息渲染不变。
 */
import { screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { buildReferenceBlock } from '@/services/references/referenceProviders'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

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

describe('引用消息渲染（源无关）', () => {
  it('非 godot source 渲染「{source} 引用」与 {source}-node 徽章', () => {
    const block = buildReferenceBlock({
      source: 'figma',
      items: [{ name: 'Login 页', type: 'frame', path: 'figma://login' }],
    })
    renderWithProviders(<MessageItem message={makeUserMessage(block!)} />)
    expect(screen.getByText(/figma 引用/)).toBeInTheDocument()
    expect(screen.getByText('figma-node')).toBeInTheDocument()
    expect(screen.getByText('Login 页')).toBeInTheDocument()
  })

  it('godot 历史消息渲染不变（source + scene 后缀）', () => {
    const block = buildReferenceBlock({
      source: 'godot',
      attrs: { scene: 'res://main.tscn' },
      items: [{ name: 'Player', type: 'Node2D', path: '/root/Player' }],
    })
    renderWithProviders(<MessageItem message={makeUserMessage(block!)} />)
    expect(screen.getByText(/godot 引用 · res:\/\/main\.tscn/)).toBeInTheDocument()
    expect(screen.getByText('godot-node')).toBeInTheDocument()
  })
})
