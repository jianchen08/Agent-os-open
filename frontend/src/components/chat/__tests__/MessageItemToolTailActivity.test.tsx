// @feature: FP-0.2.工具尾活性 工具尾部活性指示 | @ci: frontend-test
/**
 * 底部活动行测试（2026-11-19 用户规格三覆盖 2026-10-19 旧裁决）
 *
 * 新契约：只要消息处于流式（思考输出中 / 工具运行中 / 正文产出中），底部恒有
 * 「执行中」指示；旧契约的「切思考中...」「有可见内容即退场」已作废——它们会让
 * 界面出现「内容还在输出但看起来已完成」。
 * - 任意流式 parts 组合 → 「执行中」
 * - 终态（isGenerating=false）/ 非尾消息 / 审批等待态 → 无指示
 * - 压缩中 → 「压缩中」
 */
import { act, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { MessagePart, TextPart, ThinkingPart, ToolCallPart } from '@/types/messageParts'
import type { Message } from '@/types/models'

vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())

// 可变 store 桩：用例内改写 pendingInteractions（审批等待态对照）
let pendingInteractions: Array<Record<string, unknown>> = []
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions }),
}))

vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/fileLoaderRegistry', async () => (await import('./helpers/messageItemMocks')).attachmentOpenerMock())
// 片段渲染桩掉：指示行是 MessageContentRenderer 的兄弟节点，断言不依赖片段本体
vi.mock('@/components/chat/MessageContentRenderer', () => ({ default: () => null }))
vi.mock('@/components/chat/MessageActions', () => ({ MessageActions: () => null }))
// useMessageRender 保持真实实现：isStreaming 门（isGenerating && isLast && assistant）
// 是被测行为的一部分

function makeMessage(parts: MessagePart[], overrides: Partial<Message> = {}): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    status: 'streaming',
    parts,
    ...overrides,
  } as Message
}

function textPart(content: string, state: TextPart['state'] = 'done'): TextPart {
  return { type: 'text', content, state }
}

function toolPart(state: ToolCallPart['state'], callId = 'c1'): ToolCallPart {
  return { type: 'tool_call', callId, name: 'search', args: {}, state }
}

function thinkingPart(content: string, state: ThinkingPart['state'] = 'streaming'): ThinkingPart {
  return { type: 'thinking', content, state }
}

function renderTail(
  parts: MessagePart[],
  props: { isLast?: boolean; isGenerating?: boolean; streamAnchorMs?: number | null } = {},
) {
  return renderWithProviders(
    <MessageItem
      message={makeMessage(parts)}
      isLast={props.isLast ?? true}
      isGenerating={props.isGenerating ?? true}
      streamAnchorMs={props.streamAnchorMs}
    />,
  )
}

describe('工具尾部活性指示（2026-10-19）', () => {
  afterEach(() => {
    pendingInteractions = []
  })

  it('工具执行中：卡下显示「执行中」', () => {
    renderTail([textPart('先查一下'), toolPart('calling')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('工具已完成、等 LLM 推送的间隙：仍显示「执行中」（流式未收尾即如实反映）', () => {
    renderTail([textPart('先查一下'), toolPart('done')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('模型推送到达（tool 后出现空 thinking 占位）：仍显示「执行中」', () => {
    renderTail([toolPart('done'), thinkingPart('')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('模型推送到达（tool 后出现空 text 占位）：仍显示「执行中」', () => {
    renderTail([toolPart('done'), textPart('', 'streaming')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('思考内容到达（tool 后 thinking 非空）：仍显示「执行中」（4542fa22da 起不按阶段切文案）', () => {
    renderTail([toolPart('done'), thinkingPart('分析工具结果…')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
    expect(screen.getByTestId('tool-tail-activity')).not.toHaveTextContent('思考中')
  })

  it('正文流式输出中（tool 后 text 非空）：指示不退场，恒「执行中」（底部活动行不按阶段退场）', () => {
    renderTail([toolPart('done'), textPart('根据结果，答案是…', 'streaming')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('多工具链尾卡同理：最后一个 tool_call 之后无推送 → 「执行中」', () => {
    renderTail([toolPart('done', 'c1'), toolPart('calling', 'c2')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
  })

  it('非生成态（isGenerating=false）：不显示', () => {
    renderTail([toolPart('done')], { isGenerating: false })
    expect(screen.queryByTestId('tool-tail-activity')).not.toBeInTheDocument()
  })

  it('非尾消息（isLast=false）：不显示', () => {
    renderTail([toolPart('done')], { isLast: false })
    expect(screen.queryByTestId('tool-tail-activity')).not.toBeInTheDocument()
  })

  it('同会话审批等待态：不显示（等待用户归交互卡，执行中是误报）', () => {
    pendingInteractions = [{ threadId: 'session-1', status: 'pending' }]
    renderTail([toolPart('calling')])
    expect(screen.queryByTestId('tool-tail-activity')).not.toBeInTheDocument()
  })

  it('无工具的纯流式消息：空内容占位显「执行中」，底部行不重复渲染', () => {
    renderWithProviders(
      <MessageItem message={makeMessage([])} isLast isGenerating />,
    )
    expect(screen.queryByTestId('tool-tail-activity')).not.toBeInTheDocument()
    expect(screen.getByText('执行中')).toBeInTheDocument()
  })

  it('压缩中：空内容占位显示「压缩中」', () => {
    act(() => {
      usePipelineMessageStore.setState({ activePipelineId: 'pipe-c' })
      usePipelineMessageStore.getState().setCompressing('pipe-c', true)
    })
    renderWithProviders(<MessageItem message={makeMessage([])} isLast isGenerating />)
    expect(screen.getByText('压缩中')).toBeInTheDocument()
    act(() => usePipelineMessageStore.getState().setCompressing('pipe-c', false))
  })

  it('压缩中：底部活动行切「压缩中」（压过执行中）', () => {
    act(() => {
      usePipelineMessageStore.setState({ activePipelineId: 'pipe-c2' })
      usePipelineMessageStore.getState().setCompressing('pipe-c2', true)
    })
    renderTail([toolPart('done'), thinkingPart('')])
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('压缩中')
    act(() => usePipelineMessageStore.getState().setCompressing('pipe-c2', false))
  })

  it('压缩结束后指示回到「执行中」', () => {
    act(() => {
      usePipelineMessageStore.setState({ activePipelineId: 'pipe-c3' })
      usePipelineMessageStore.getState().setCompressing('pipe-c3', true)
    })
    renderWithProviders(<MessageItem message={makeMessage([])} isLast isGenerating />)
    expect(screen.getByText('压缩中')).toBeInTheDocument()
    act(() => usePipelineMessageStore.getState().setCompressing('pipe-c3', false))
    expect(screen.getByText('执行中')).toBeInTheDocument()
  })

  // 结构契约：指示行 = 旋转图标 + 状态文案（与既有「思考中」空态指示同款形态）
  it('指示行含旋转图标（animate-spin），流式期间文案恒为「执行中」', () => {
    const view = renderTail([toolPart('done')])
    expect(view.getByTestId('tool-tail-activity').querySelector('.animate-spin')).toBeInTheDocument()

    view.rerender(
      <MessageItem
        message={makeMessage([toolPart('done'), thinkingPart('')])}
        isLast
        isGenerating
      />,
    )
    expect(screen.getByTestId('tool-tail-activity')).toHaveTextContent('执行中')
    expect(screen.getByTestId('tool-tail-activity').querySelector('.animate-spin')).toBeInTheDocument()
  })

  // 2026-11-06 用户裁定：指示行统一图标后跟实时总耗时（自触发消息发出起计）
  it('带耗时锚点：指示行显示自触发消息起的实时总耗时', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-11-06T10:00:10Z'))
      renderTail([textPart('先查一下'), toolPart('calling')], {
        streamAnchorMs: Date.parse('2026-11-06T10:00:07.5Z'),
      })
      expect(screen.getByTestId('stream-elapsed')).toHaveTextContent('2.5s')
    } finally {
      vi.useRealTimers()
    }
  })

  it('空内容占位「执行中」同款带实时总耗时；无锚点不显示耗时', () => {
    vi.useFakeTimers()
    try {
      vi.setSystemTime(new Date('2026-11-06T10:00:10Z'))
      const withAnchor = renderWithProviders(
        <MessageItem
          message={makeMessage([])}
          isLast
          isGenerating
          streamAnchorMs={Date.parse('2026-11-06T10:00:08Z')}
        />,
      )
      expect(withAnchor.getByText('执行中')).toBeInTheDocument()
      expect(withAnchor.getByTestId('stream-elapsed')).toHaveTextContent('2.0s')
      withAnchor.unmount()

      const withoutAnchor = renderWithProviders(
        <MessageItem message={makeMessage([])} isLast isGenerating />,
      )
      expect(withoutAnchor.getByText('执行中')).toBeInTheDocument()
      expect(withoutAnchor.queryByTestId('stream-elapsed')).not.toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })
})
