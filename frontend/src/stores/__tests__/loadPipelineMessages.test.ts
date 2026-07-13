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
      reconciledByPipeline: {},
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

  it("mode='auto' 已对账 → 不做任何 API 调用，直接用缓存", async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'running', parentId: null, unreadCount: 0,
    })
    // 先全量 init 建立本地状态 + bottomCursor=2，并标记已对账
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    usePipelineMessageStore.setState({ reconciledByPipeline: { [PIPELINE_ID]: true } })
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 已对账：不发起任何 API 请求，消息数不变
    expect(mockGet).not.toHaveBeenCalled()
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
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

  it('rehydrate 后（reconciled 缺失）即使 isInitialized=true 也走全量 init', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    // 模拟 rehydrate 后状态：本地有消息 + bottomCursor 已恢复，但 reconciledByPipeline 为空
    // （merge 中重置为 {}）。此时 isInitialized=true，但未对账 → 必须走全量 init 而非增量补漏。
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])
    usePipelineMessageStore.setState({ reconciledByPipeline: {} })
    expect(store.isInitialized(PIPELINE_ID)).toBe(true)
    expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBeFalsy()

    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 关键断言：未对账 → 全量请求，不传 after_sequence（增量补漏拉不到已加载区间内的空洞）
    expect(mockGet).toHaveBeenCalledTimes(1)
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBeUndefined()
    // 对账成功后标记
    expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBe(true)
  })

  it('首次 auto 全量对账后，后续 auto 走 after_sequence 增量补漏', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // 第一次 auto：未对账 → 全量 init
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
    ])
    await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })
    const firstCallArg = mockGet.mock.calls[0][1]
    expect(firstCallArg.params.after_sequence).toBeUndefined()
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)

    // 第二次 auto：已对账 → 不做任何 API 调用，直接用缓存
    // （不设 setApiRecords，验证 mockGet 不再被调用）
    await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })
    // mockGet 只被调用一次（第一次全量对账），第二次不做请求
    expect(mockGet).toHaveBeenCalledTimes(1)
    // 消息数不变
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
  })

  it('流式断线空洞：rehydrate 后全量对账修正已加载区间内的缺失消息（核心回归）', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    // 模拟流式断线残留：本地有 seq1(user) + seq2(assistant 空气泡，WS 生成的 id，
    // rehydrate 把 status:'streaming' 改为 'completed')。bottomCursor 被推到 2。
    // 刷新后若走增量补漏 after_sequence=2，永远拉不到 seq≤2 的修正 + 断线期间后续消息。
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        [PIPELINE_ID]: [
          makeMsg('u1', 1, { role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' }),
          makeMsg('ws-stream-uuid-a1', 2, { content: '', status: 'completed', timestamp: '2026-01-01T00:00:01Z' }),
        ],
      },
      bottomCursorsByPipeline: { [PIPELINE_ID]: 2 },
      reconciledByPipeline: {},
    })
    expect(store.isInitialized(PIPELINE_ID)).toBe(true)

    // 后端权威：seq2 实际有完整内容（修正空气泡），seq3-4 是断线期间用户继续对话的后续消息。
    // 后端 id（hex）与本地 WS uuid 不同，靠 mergeApiWithExisting 丢弃本地残缺 + 指纹去重。
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'hex-a1', sequence: 2, role: 'assistant', content: '完整的AI回复', timestamp: '2026-01-01T00:00:01Z' },
      { id: 'u2', sequence: 3, role: 'user', content: 'q2', timestamp: '2026-01-01T00:00:02Z' },
      { id: 'hex-a2', sequence: 4, role: 'assistant', content: 'a2', timestamp: '2026-01-01T00:00:03Z' },
    ])

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 必须走全量 init（无 after_sequence），否则补不到 seq2 的修正与 seq3-4 后续消息
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBeUndefined()

    const msgs = store.getMessages(PIPELINE_ID)
    const sequences = msgs.map((m) => m.sequence)
    // 全量对账后 4 条消息全部到位
    expect(sequences).toEqual([1, 2, 3, 4])
    // seq2 内容被后端权威版本修正（不再是空气泡）
    const fixedMsg = msgs.find((m) => m.sequence === 2)
    expect(fixedMsg?.content).toBe('完整的AI回复')
    // 指纹去重：不出现重复气泡（每个 sequence 只有一条）
    expect(sequences.length).toBe(new Set(sequences).size)
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(4)
  })
})
