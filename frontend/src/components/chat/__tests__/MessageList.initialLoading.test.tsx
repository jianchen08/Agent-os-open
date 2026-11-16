// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * MessageList 首屏加载骨架测试（#5/#6）：
 * - store 级 initialLoadingByPipeline（会话切换首屏 GET await 窗口）置位 →
 *   消息区渲染骨架，不闪「开始新的对话」空态（空 ≠ 加载）
 * - agentTabStore.tabMessagesLoading（子 Tab 首载，此前全仓零消费者）→
 *   子标签消息区同走骨架
 * - 两标记皆空 → 保持欢迎空态（真空态不吞）
 * - store 侧：fetchMessages 全量 GET await 窗口置位/落定清除（成功/失败皆清）
 */
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { MessageList } from '../MessageList'

// 段清单是 MessageList 挂载旁路面（activePipelineId 在场即拉取），桩为空清单
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))
// 网络层 mock（store 侧 fetchMessages 用例）
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))
vi.mock('@/utils/logger', async () =>
  (await import('@/stores/__tests__/helpers/storeTestMocks')).loggerMockFull(),
)
vi.mock('@/utils/retry', async () =>
  (await import('@/stores/__tests__/helpers/storeTestMocks')).retryMockFull(),
)

const baseProps = { isGenerating: false, className: '' } as const

beforeEach(() => {
  vi.clearAllMocks()
  mockGet.mockResolvedValue({ data: { messages: [], total: 0, has_more: false } })
  usePipelineMessageStore.setState({
    activePipelineId: null,
    loadErrorByPipeline: {},
    initialLoadingByPipeline: {},
    messagesByPipeline: {},
    spanIndex: {},
    segmentViewByPipeline: {},
  })
  useAgentTabStore.setState({ tabMessagesLoading: {} })
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MessageList 首屏加载骨架（空 ≠ 加载）', () => {
  it('initialLoadingByPipeline 置位 → 骨架上屏，不渲染空态文案', () => {
    usePipelineMessageStore.setState({
      activePipelineId: 'p1',
      initialLoadingByPipeline: { p1: true },
    })
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" />)

    expect(screen.getByTestId('message-list-initial-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-empty')).not.toBeInTheDocument()
    expect(screen.queryByText('开始新的对话')).not.toBeInTheDocument()
  })

  it('tabMessagesLoading 置位（子 Tab 首载）→ 同走骨架', () => {
    useAgentTabStore.setState({ tabMessagesLoading: { 'sub-1': true } })
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" tabId="sub-1" />)

    expect(screen.getByTestId('message-list-initial-loading')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-empty')).not.toBeInTheDocument()
  })

  it('两标记皆空 → 保持欢迎空态（真空态不被吞）', () => {
    usePipelineMessageStore.setState({ activePipelineId: 'p1' })
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" tabId="main-1" />)

    expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('message-list-initial-loading')).not.toBeInTheDocument()
  })

  it('标记按管道/Tab 键控：他键置位不影响本视图', () => {
    usePipelineMessageStore.setState({
      activePipelineId: 'p1',
      initialLoadingByPipeline: { 'p-other': true },
    })
    useAgentTabStore.setState({ tabMessagesLoading: { 'sub-other': true } })
    render(<MessageList {...baseProps} messages={[]} sessionId="s1" tabId="main-1" />)

    expect(screen.getByTestId('message-list-empty')).toBeInTheDocument()
  })
})

describe('pipelineMessageStore initialLoadingByPipeline（fetchMessages await 窗口）', () => {
  const PIPELINE_ID = 'pipe-init'
  const THREAD_ID = 'thread-init'

  async function freshStore() {
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    return mod.usePipelineMessageStore
  }

  it('全量 GET await 窗口置位，落定清除', async () => {
    const store = await freshStore()
    const s = store.getState()
    s.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    let resolveGet: (v: unknown) => void = () => {}
    mockGet.mockReturnValue(new Promise((resolve) => { resolveGet = resolve }))

    const promise = store.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    expect(store.getState().initialLoadingByPipeline[PIPELINE_ID]).toBe(true)

    resolveGet({ data: { messages: [], total: 0, has_more: false } })
    await promise
    expect(store.getState().initialLoadingByPipeline[PIPELINE_ID]).toBe(false)
  })

  it('翻页/补漏请求（带游标）不置首载标记', async () => {
    const store = await freshStore()
    const s = store.getState()
    s.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    await store.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, before_sequence: 10 })
    expect(store.getState().initialLoadingByPipeline[PIPELINE_ID] ?? false).toBe(false)
    await store.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: 0 })
    expect(store.getState().initialLoadingByPipeline[PIPELINE_ID] ?? false).toBe(false)
  })

  it('失败路径标记同样清除（骨架退场，失败走 loadError 错误态）', async () => {
    const store = await freshStore()
    const s = store.getState()
    s.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    mockGet.mockRejectedValueOnce(new Error('net down'))
    await expect(
      store.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID }),
    ).rejects.toThrow('net down')
    expect(store.getState().initialLoadingByPipeline[PIPELINE_ID]).toBe(false)
  })
})
