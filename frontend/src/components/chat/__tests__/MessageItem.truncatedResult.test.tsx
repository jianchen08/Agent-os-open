/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 工具消息「结果已截断」渲染补测（数据面瘦身批次 1.2）：
 * - 结果体是截断标记（预览 + 标记）时，卡内出现「结果已截断…显示与 LLM
 *   所见一致」详情块，标记对象本身不进渲染内容；
 * - 展开详情块显示与 LLM 所见一致的预览（2026-11-18 用户裁定：展开纯渲染，
 *   不发起回取请求——缺省不接线 onExpand，拿不到的数据本就不该拿）。
 *
 * mock 边界与 MessageItemToolCard 同族（重依赖子面桩化），被测分支走真实链路。
 */

import { fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { loadRenderIntents } from '@/utils/renderIntent'
import { MessageItem } from '../MessageItem'
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

const TRUNCATED_MARK = {
  __truncated: true as const,
  preview: '{"rows": "预览内容"}',
  totalBytes: 32_768,
  sequence: 2,
}

function makeTruncatedToolMessage(): Message {
  return {
    id: 'tool-trunc',
    sessionId: 'session-1',
    sequence: 2,
    role: 'tool',
    content: '',
    timestamp: '2026-10-28T00:00:00Z',
    status: 'completed',
    toolName: 'search',
    toolResult: TRUNCATED_MARK,
  } as Message
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  loadRenderIntents([])
})

describe('MessageItem — 截断结果的预览与展开', () => {
  it('截断标记 → 卡内出现「结果已截断」块，标记对象本身不上屏', async () => {
    renderWithProviders(<MessageItem message={makeTruncatedToolMessage()} />)

    // 展开卡片看详情
    fireEvent.click(screen.getByText('Search'))

    const label = await screen.findByText(/结果已截断（共 32\.0 KB），显示与 LLM 所见一致/)
    expect(label).toBeInTheDocument()
    expect(screen.queryByText(/__truncated/)).not.toBeInTheDocument()
  })

  it('点开详情块 → 显示与 LLM 所见一致的预览（纯渲染，不回取全文）', async () => {
    renderWithProviders(<MessageItem message={makeTruncatedToolMessage()} />)
    fireEvent.click(screen.getByText('Search'))

    fireEvent.click(await screen.findByText(/结果已截断（共 32\.0 KB），显示与 LLM 所见一致/))

    expect(screen.getByText('{"rows": "预览内容"}')).toBeInTheDocument()
    // 2026-11-18 裁定撤销了回取接线：通用工具卡的截断展开不再有失败/回退路径
    expect(screen.queryByText('完整结果加载失败，当前显示预览')).not.toBeInTheDocument()
  })
})
