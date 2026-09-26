// @feature FP-T12 前端组件补测
/**
 * 工具消息未知状态渲染行为测试
 *
 * 状态词表映射不上的未知值不猜 completed（未知 ≠ 成功）：
 * - 卡片按 pending 渲染
 * - 同一未知值只 console.warn 一次
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { loadRenderIntents } from '@/utils/renderIntent'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())

vi.mock('@/stores/agentStore', async () => (await import('./helpers/messageItemMocks')).agentStoreEmptyMock())

vi.mock('@/stores/interactionStore', async () => (await import('./helpers/messageItemMocks')).interactionStoreEmptyMock())

vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())

vi.mock('@/services/attachmentOpener', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())

vi.mock('@/components/chat/MessageActions', async () => (await import('./helpers/messageItemMocks')).messageActionsStubMock())

vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

vi.mock('@/components/chat/MessageContentRenderer', async () => (await import('./helpers/messageItemMocks')).messageContentRendererStubMock())

vi.mock('@/components/chat/hooks/useMessageRender', async () => (await import('./helpers/messageItemMocks')).useMessageRenderStubMock())

function makeToolMessage(status: string, id: string): Message {
  return {
    id,
    sessionId: 'session-1',
    sequence: 2,
    role: 'tool',
    content: '',
    timestamp: new Date().toISOString(),
    status,
    toolName: 'search',
    toolResult: '结果',
  } as Message
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => loadRenderIntents([]))

describe('MessageItem 工具消息未知状态', () => {
  it('未知状态按 pending 渲染，不标成 completed', () => {
    renderWithProviders(<MessageItem message={makeToolMessage('teleported', 't1')} />)

    const card = document.querySelector('[data-activity-status]')
    expect(card).not.toBeNull()
    expect(card!.getAttribute('data-activity-status')).toBe('pending')
  })

  it('同一未知状态值只 console.warn 一次', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    renderWithProviders(
      <>
        <MessageItem message={makeToolMessage('migrated', 't2')} />
        <MessageItem message={makeToolMessage('migrated', 't3')} />
      </>,
    )

    const warns = warnSpy.mock.calls.filter((args) =>
      args.some((a) => String(a).includes('migrated')),
    )
    expect(warns).toHaveLength(1)
    warnSpy.mockRestore()
  })

  it('词表内状态不触发警告（completed 正常映射）', () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})

    renderWithProviders(<MessageItem message={makeToolMessage('completed', 't4')} />)

    expect(document.querySelector('[data-activity-status="completed"]')).not.toBeNull()
    expect(warnSpy).not.toHaveBeenCalled()
    warnSpy.mockRestore()
  })
})
