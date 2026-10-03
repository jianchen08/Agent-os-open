// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * MessageItem 骨架形态特征化测试（消息流渲染器化改造护栏，2026-10-03）。
 *
 * 锁四类 role 的分支骨架可观察形态——改造（卡片分发器迁移）中这些断言
 * 一字不改保持绿 = 布局形态未变：
 * - user：行容器反向（flex-row-reverse）+ 内容列右对齐（items-end）+ 80% 宽约束
 * - assistant：正向行 + 左对齐 + 气泡宽约束（calc(100%-44px) + flex-1）
 * - tool / system：data-role 标记 + 与 assistant 同款宽度约束
 * - 恒有 data-testid="message-item"（测试与 e2e 的定位锚）
 *
 * 内容渲染（markdown/分片/工具卡内部）归各自专项测试，此处一律桩掉。
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MessageItem } from '../MessageItem'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { Message } from '@/types/models'

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())

vi.mock('@/stores/agentStore', async () => (await import('./helpers/messageItemMocks')).agentStoreEmptyMock())

vi.mock('@/stores/interactionStore', async () => (await import('./helpers/messageItemMocks')).interactionStoreEmptyMock())

vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())

vi.mock('@/services/fileLoaderRegistry', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())

vi.mock('@/components/chat/MessageActions', async () => (await import('./helpers/messageItemMocks')).messageActionsStubMock())

vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

vi.mock('@/components/chat/MessageContentRenderer', async () => (await import('./helpers/messageItemMocks')).messageContentRendererStubMock())

vi.mock('@/components/chat/hooks/useMessageRender', async () => (await import('./helpers/messageItemMocks')).useMessageRenderStubMock())

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'msg-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'user',
    content: 'hello',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

function renderMessage(message: Message) {
  return renderWithProviders(
    <MessageItem message={message} isLast={false} isGenerating={false} />,
  )
}

describe('MessageItem 骨架形态（role 分支特征化）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('user：行容器反向 + 内容列右对齐 + 80% 宽约束', () => {
    const { container } = renderMessage(makeMessage({ role: 'user' }))
    const item = screen.getByTestId('message-item')
    expect(item).toHaveAttribute('data-role', 'user')
    // 行容器即 testid 元素：头像与内容列反向排布（用户在右）
    expect(item.className).toContain('flex-row-reverse')
    // 内容列：右对齐 + 用户气泡最大 80% 宽
    const column = item.querySelector('.items-end')
    expect(column).not.toBeNull()
    expect(column!.className).toContain('max-w-[80%]')
    void container
  })

  it('assistant：正向行 + 内容列左对齐 + 气泡宽约束（calc(100%-44px) + flex-1）', () => {
    renderMessage(makeMessage({ role: 'assistant', content: 'reply' }))
    const item = screen.getByTestId('message-item')
    expect(item).toHaveAttribute('data-role', 'assistant')
    expect(item.className).not.toContain('flex-row-reverse')
    const column = item.querySelector('.items-start')
    expect(column).not.toBeNull()
    expect(column!.className).toContain('max-w-[calc(100%-44px)]')
    expect(column!.className).toContain('flex-1')
  })

  it('tool：data-role=tool，宽度约束在容器自身（与 assistant 内容列同款值）', () => {
    renderMessage(
      makeMessage({
        role: 'tool',
        content: '',
        toolName: 'search',
        toolResult: '结果',
      }),
    )
    const item = screen.getByTestId('message-item')
    expect(item).toHaveAttribute('data-role', 'tool')
    expect(item.className).toContain('max-w-[calc(100%-44px)]')
  })

  it('system：data-role=system', () => {
    renderMessage(makeMessage({ role: 'system', content: 'notice' }))
    expect(screen.getByTestId('message-item')).toHaveAttribute('data-role', 'system')
  })
})
