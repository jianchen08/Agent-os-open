/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * MessageList 会话切换加载失败错误态测试（X1-F1）
 *
 * 行为契约：会话切换时 loadPipelineMessages 失败（sessionListStore 落
 * loadErrorByPipeline 标记）后，消息区必须渲染错误提示 + 「重新加载」按钮，
 * 而非误导性的「开始新的对话」欢迎空态；点击重试复用统一加载入口
 * loadPipelineMessages（mode 默认 auto）。
 */

import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { MessageList } from '../MessageList'

// 段清单是 MessageList 挂载旁路面（activePipelineId 在场即拉取），桩为空清单
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))

// Mock MessageItem（错误态家族用例不深入渲染消息行）
vi.mock('../MessageItem', () => ({
  MessageItem: ({ message }: { message: { id: string } }) => (
    <div data-testid={`message-item-${message.id}`} />
  ),
}))

beforeEach(() => {
  vi.clearAllMocks()
  usePipelineMessageStore.setState({
    activePipelineId: null,
    loadErrorByPipeline: {},
    messagesByPipeline: {},
    spanIndex: {},
    segmentViewByPipeline: {},
  })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function seedLoadError(activePipelineId: string, failedPipelineId: string) {
  usePipelineMessageStore.setState({
    activePipelineId,
    loadErrorByPipeline: { [failedPipelineId]: true },
  })
}

describe('MessageList — 会话切换加载失败错误态（X1-F1）', () => {
  const baseProps = { isGenerating: false, className: '' } as const

  it('当前管道加载失败时渲染错误提示与重新加载按钮（不渲染欢迎空态）', () => {
    seedLoadError('p1', 'p1')
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" />)

    expect(screen.getByTestId('message-list-load-error')).toBeInTheDocument()
    expect(screen.getByText('消息加载失败')).toBeInTheDocument()
    expect(screen.getByTestId('message-list-retry-load')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-empty')).not.toBeInTheDocument()
    expect(screen.queryByText('开始新的对话')).not.toBeInTheDocument()
  })

  it('无失败标记时保持欢迎空态（错误态只由失败标记驱动）', () => {
    usePipelineMessageStore.setState({ activePipelineId: 'p1' })
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" />)

    expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-load-error')).not.toBeInTheDocument()
  })

  it('失败标记属于其它管道时不渲染错误态（标记按管道键控）', () => {
    seedLoadError('p1', 'p-other')
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" />)

    expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-load-error')).not.toBeInTheDocument()
  })

  it('点击重新加载按钮以统一加载入口重载当前管道（threadId=会话 id）', () => {
    seedLoadError('p1', 'p1')
    const loadSpy = vi
      .spyOn(usePipelineMessageStore.getState(), 'loadPipelineMessages')
      .mockResolvedValue({ ok: true })

    render(<MessageList {...baseProps} messages={[]} sessionId="s1" />)
    fireEvent.click(screen.getByTestId('message-list-retry-load'))

    expect(loadSpy).toHaveBeenCalledTimes(1)
    expect(loadSpy).toHaveBeenCalledWith('p1', { threadId: 's1' })
    loadSpy.mockRestore()
  })
})
