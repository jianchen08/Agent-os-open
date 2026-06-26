/**
 * loadPipelineMessages 统一加载入口测试
 *
 * 覆盖收敛后的 4 种加载场景：
 *  - mode='auto' 未初始化 → 全量 init
 *  - mode='auto' 已初始化 → after_sequence 增量补漏
 *  - mode='backfill' 强制增量（WS 重连）
 *  - skipStreamingCheck：流式中跳过 vs 无条件补漏
 *  - 异常传播：底层 fetchMessages 失败 → { ok:false, error }
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

// mock apiClient.get（网络层）
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

vi.mock('@/utils/retry', () => ({
  requestWithRetry: async (fn: () => Promise<any>) => fn(),
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

/** 设置 apiClient.get 返回的后端原始 records */
function setApiRecords(records: any[], hasMore = false) {
  mockGet.mockResolvedValueOnce({ data: { messages: records, total: records.length, has_more: hasMore } })
}

const PIPELINE_ID = 'pipe-load-001'
const THREAD_ID = 'thread-load-001'

function makeMsg(id: string, seq: number, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: THREAD_ID,
    sequence: seq,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('loadPipelineMessages 统一加载入口', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
  })

  it("mode='auto' 未初始化 → 全量 init（无 after_sequence）", async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 全量 init 应设置 bottomCursor
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
    // 确认没传 after_sequence（全量而非补漏）
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it("mode='auto' 已初始化 → after_sequence 增量补漏", async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'running', parentId: null, unreadCount: 0,
    })
    // 先全量 init 建立本地状态 + bottomCursor=2
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)

    // 补漏：API 返回 seq>2 的新消息
    setApiRecords([
      { id: 'u2', sequence: 3, role: 'user', content: 'q2', timestamp: '2026-01-01T00:00:02Z' },
      { id: 'a2', sequence: 4, role: 'assistant', content: 'a2', timestamp: '2026-01-01T00:00:03Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(4)
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(4)
    // 确认传了 after_sequence=2（增量而非全量）
    expect(mockGet).toHaveBeenCalledTimes(1)
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBe(2)
  })

  it('流式输出中（count>1）且未 skipStreamingCheck → 跳过加载', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'running', parentId: null, unreadCount: 0,
    })
    // 模拟流式输出：已有 2 条消息 + 正在流式
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    store.startStreaming(PIPELINE_ID, 'streaming-msg-1')

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 流式保护：不应发起 API 请求
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('skipStreamingCheck=true → 流式中仍无条件补漏（WS 重连场景）', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'running', parentId: null, unreadCount: 0,
    })
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    store.startStreaming(PIPELINE_ID, 'streaming-msg-1')
    setApiRecords([
      { id: 'a2', sequence: 3, role: 'assistant', content: 'a2', timestamp: '2026-01-01T00:00:02Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      mode: 'backfill',
      skipStreamingCheck: true,
    })

    expect(result.ok).toBe(true)
    // 无条件补漏：应发起请求且传 after_sequence
    expect(mockGet).toHaveBeenCalledTimes(1)
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBe(2)
  })

  it('底层 fetchMessages 失败 → 返回 { ok:false, error } 不吞异常', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    const apiError = Object.assign(new Error('服务器错误'), {
      response: { status: 500 },
    })
    mockGet.mockRejectedValueOnce(apiError)

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(false)
    expect(result.error).toBe(apiError)
  })

  it("mode='init' 强制全量（即使已初始化）", async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    // 已初始化（有 bottomCursor）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
      { id: 'a2', sequence: 3, role: 'assistant', content: 'a2', timestamp: '2026-01-01T00:00:02Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      mode: 'init',
    })

    expect(result.ok).toBe(true)
    // mode='init' 不传 after_sequence（强制全量）
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBeUndefined()
  })
})
