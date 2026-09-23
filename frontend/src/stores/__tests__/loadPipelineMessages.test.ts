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
import { makePipelineMsgFactory, resetPipelineStoreState } from './helpers/storeTestMocks'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'

// mock apiClient.get（网络层）
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

// 段清单是 loadPipelineMessages 的并行旁路面（消息段模型），不归本文件
// mode 决策断言管——桩定为空清单，避免吃掉 mockGet 的 once 队列
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))

vi.mock('@/utils/logger', async () => (await import('./helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/utils/retry', async () => (await import('./helpers/storeTestMocks')).retryMockFull())

/** 设置 apiClient.get 返回的后端原始 records */
function setApiRecords(records: any[], hasMore = false) {
  mockGet.mockResolvedValueOnce({ data: { messages: records, total: records.length, has_more: hasMore } })
}

const PIPELINE_ID = 'pipe-load-001'
const THREAD_ID = 'thread-load-001'

const makeMsg = makePipelineMsgFactory(THREAD_ID)
/** 注册一条标准测试管道（status 可变），返回 store —— 本文件播种块共用 */
function registeredStore(
  usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore,
  status = 'idle',
) {
  const store = usePipelineMessageStore.getState()
  store.registerPipeline({
    pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: 'tab-1',
    agentName: '', status, parentId: null, unreadCount: 0,
  })
  return store
}

/** 播种两条基线消息（u1 seq1 user / a1 seq2 assistant）——对账/流式家族共用 */
function seedBaselineMsgs(store: ReturnType<typeof registeredStore>) {
  store.initFromAPI(PIPELINE_ID, [
    makeMsg('u1', 1, { role: 'user', content: 'q' }),
    makeMsg('a1', 2, { content: 'a' }),
  ])
  return store
}

/** 两条基线 API 记录（u1/a1，带 timestamp）——各用例按需追加差异化记录 */
function baseApiRecords() {
  return [
    { id: 'u1', sequence: 1, role: 'user', content: 'q', timestamp: '2026-01-01T00:00:00Z' },
    { id: 'a1', sequence: 2, role: 'assistant', content: 'a', timestamp: '2026-01-01T00:00:01Z' },
  ]
}

describe('loadPipelineMessages 统一加载入口', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore = await resetPipelineStoreState({ pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID } })
  })

  it("mode='auto' 未初始化 → 全量 init（无 after_sequence）", async () => {
    const store = registeredStore(usePipelineMessageStore, 'idle')
    setApiRecords(baseApiRecords())

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 全量 init 应设置 bottomCursor
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
    // 确认没传 after_sequence（全量而非补漏）
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it("mode='auto' 已对账 → 不做任何 API 调用，直接用缓存", async () => {
    const store = registeredStore(usePipelineMessageStore, 'running')
    // 先全量 init 建立本地状态 + bottomCursor=2，并标记已对账
    seedBaselineMsgs(store)
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
    const store = registeredStore(usePipelineMessageStore, 'running')
    // 模拟流式输出：已有 2 条消息 + 正在流式
    seedBaselineMsgs(store)
    store.startStreaming(PIPELINE_ID, 'streaming-msg-1')

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 流式保护：不应发起 API 请求
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('skipStreamingCheck=true → 流式中仍无条件补漏（WS 重连场景）', async () => {
    const store = seedBaselineMsgs(registeredStore(usePipelineMessageStore, 'running'))
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
    const store = registeredStore(usePipelineMessageStore, 'idle')
    const apiError = Object.assign(new Error('服务器错误'), {
      response: { status: 500 },
    })
    mockGet.mockRejectedValueOnce(apiError)

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(false)
    expect(result.error).toBe(apiError)
  })

  it("mode='init' 强制全量（即使已初始化）", async () => {
    const store = registeredStore(usePipelineMessageStore, 'idle')
    // 已初始化（有 bottomCursor）
    seedBaselineMsgs(store)
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

  it('rehydrate 后（reconciled 缺失）有本地缓存 → 缓存秒开 + 后台全量对账', async () => {
    const store = registeredStore(usePipelineMessageStore, 'idle')
    // 模拟 rehydrate 后状态：本地有消息 + bottomCursor 已恢复，但 reconciledByPipeline 为空
    // （merge 中重置为 {}）。此时 isInitialized=true，但未对账。
    seedBaselineMsgs(store)
    usePipelineMessageStore.setState({ reconciledByPipeline: {} })
    expect(store.isInitialized(PIPELINE_ID)).toBe(true)
    expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBeFalsy()

    setApiRecords(baseApiRecords())

    const result = await store.loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    expect(result.ok).toBe(true)
    // 本地有缓存：同步返回，页面立即用缓存渲染（不发请求、不等待对账）
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
    // 后台静默全量对账被触发（2026-08-22：刷新后缓存秒开 + 对账修正空洞/残影）
    await vi.waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(1)
    })
    // 关键断言：对账走全量请求，不传 after_sequence（增量补漏拉不到已加载区间内的空洞）
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBeUndefined()
    // 对账完成后标记
    await vi.waitFor(() => {
      expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBe(true)
    })
  })

  it('首次 auto 全量对账后，后续 auto 走 after_sequence 增量补漏', async () => {
    const store = registeredStore(usePipelineMessageStore, 'idle')

    // 第一次 auto：未对账 → 全量 init
    setApiRecords(baseApiRecords())
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
    const store = registeredStore(usePipelineMessageStore, 'idle')
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
    // 本地有缓存：loadPipelineMessages 同步返回（不等对账），缓存立即渲染
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
    // 后台全量对账修正空洞（2026-08-22：initFromAPI 权威替换，不能只 after_sequence 增量）
    await vi.waitFor(() => {
      expect(mockGet).toHaveBeenCalledTimes(1)
    })
    // 关键断言：对账走全量请求，不传 after_sequence，否则补不到 seq2 的修正与 seq3-4 后续消息
    const callArg = mockGet.mock.calls[0][1]
    expect(callArg.params.after_sequence).toBeUndefined()
    // 等待后台对账完成（initFromAPI 权威替换落地）
    await vi.waitFor(() => {
      expect(store.getMessages(PIPELINE_ID).length).toBe(4)
    })

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

// ============================================================
// 向上翻页 × 断线补漏 并发语义（BUG-20）
//
// 后端消息窗口契约：init / 补漏（after_sequence，尾部锚定）与翻页
//（before_sequence，头部锚定）读取的是**不重叠的 sequence 区间**，
// 写面各自 prepend/append 互不冲突——二者并发时必须都落库。
// 回归场景（BUG-20 R46）：用户向上翻页在途时 WS 重连触发同管道补漏，
// 旧「取代即取消」策略把翻页静默掐死——用户滚动到顶看不到任何加载、
// 更早历史永远拉不到（GUI 实证「向上滚动不触发分页，无加载态」）。
// ============================================================
describe('向上翻页与断线补漏并发（BUG-20）', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore
  /** 主管道真实布局复刻（thread-c5faba0a 73 条，seq 0-72）：seq0 = 会话首条用户消息 */
  const TOTAL = 73

  function makeRange(from: number, to: number): any[] {
    const records: any[] = []
    for (let seq = from; seq <= to; seq++) {
      records.push({
        id: `m${seq}`,
        sequence: seq,
        role: seq % 2 === 0 ? 'assistant' : 'user',
        content: seq === 0 ? '请详细介绍一下长城的历史' : `msg-${seq}`,
        timestamp: new Date(Date.UTC(2026, 8, 14, 17, 25, seq)).toISOString(),
      })
    }
    return records
  }

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore = await resetPipelineStoreState({ pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID } })
  })

  it('翻页在途时同管道补漏发起：两窗不重叠，翻页不得被取消（回归钉）', async () => {
    const store = usePipelineMessageStore.getState()
    // 尾页 50 条（seq 23-72）已 init，has_more=true，topCursor=23
    store.initFromAPI(PIPELINE_ID, makeRange(23, 72), true)
    expect(store.getTopCursor(PIPELINE_ID)).toBe(23)

    // 用户滚动到顶 → 向上翻页在途（seq 0-22）
    let resolveOlder!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveOlder = resolve }),
    )
    const olderPromise = store.fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 23,
    })
    expect(usePipelineMessageStore.getState().isLoadingOlderByPipeline[PIPELINE_ID]).toBe(true)

    // 翻页在途期间 WS 重连 → 同管道补漏（seq 73-74 追加窗）
    mockGet.mockResolvedValueOnce({
      data: {
        messages: makeRange(73, 74),
        total: 2,
        has_more: false,
      },
    })
    await store.fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      after_sequence: 72,
    })
    // 补漏窗已落库（75 = 50 尾页 + 2 追加）
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(52)

    // 翻页响应到达：不得已被补漏取消——更早窗必须落库
    resolveOlder({
      data: { messages: makeRange(0, 22), total: 23, has_more: false },
    })
    await olderPromise

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(TOTAL + 2)
    const sequences = msgs.map((m) => m.sequence)
    expect(new Set(sequences).size).toBe(sequences.length)
    expect(Math.min(...sequences)).toBe(0)
    // 会话首条（含「长城」的 seq0 用户消息）必须可见
    expect(msgs.find((m) => m.sequence === 0)?.content).toContain('长城')
    expect(usePipelineMessageStore.getState().hasMoreOlderByPipeline[PIPELINE_ID]).toBe(false)
  })

  it('翻页在途被同 kind 新翻页取代：仍取消旧翻页（取代语义只作用于同窗）', async () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, makeRange(23, 72), true)

    let resolveFirst!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve }),
    )
    const firstPromise = store.fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 23,
    })

    // 同为翻页的新请求（游标更小）取代旧翻页
    mockGet.mockResolvedValueOnce({
      data: { messages: makeRange(0, 22), total: 23, has_more: false },
    })
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, before_sequence: 23 })
    // 取代者的权威窗落库：50 尾页 + 23 翻页窗 = 73
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(73)

    resolveFirst({ data: { messages: makeRange(0, 22), total: 23, has_more: false } })
    await expect(firstPromise).resolves.toBeUndefined()
    // 被取代者的迟到响应不写入（不重复）
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(73)
  })

  it('验收场景：73 条会话打开见尾页 → 一轮翻页直达 seq0，全部可见无丢失', async () => {
    const store = usePipelineMessageStore.getState()
    // 打开会话：后端尾锚定返回最新 50 条（seq 23-72），has_more=true
    mockGet.mockResolvedValueOnce({
      data: { messages: makeRange(23, 72), total: 50, has_more: true },
    })
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(50)
    expect(usePipelineMessageStore.getState().hasMoreOlderByPipeline[PIPELINE_ID]).toBe(true)

    // 滚动到顶 → 一轮翻页（before_sequence=当前最小 seq）拉回更早 23 条
    const topCursor = store.getTopCursor(PIPELINE_ID)
    expect(topCursor).toBe(23)
    mockGet.mockResolvedValueOnce({
      data: { messages: makeRange(0, 22), total: 23, has_more: false },
    })
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, before_sequence: topCursor })

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(TOTAL)
    const sequences = msgs.map((m) => m.sequence)
    // 全量可见、无重复、时序连续
    expect(sequences).toEqual(Array.from({ length: TOTAL }, (_, i) => i))
    // 首条（长城请求）已渲染进数据面
    expect(msgs[0].content).toContain('长城')
    // 已耗尽：不再触发无效翻页
    expect(usePipelineMessageStore.getState().hasMoreOlderByPipeline[PIPELINE_ID]).toBe(false)
  })
})

// ============================================================
// fetchMessages 同管道请求取消
// ============================================================
describe('fetchMessages 同管道请求取消', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore = await resetPipelineStoreState({ pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID } })
  })

  it('同 id 连续两次 fetch：第一次以取消收场且不写状态，第二次权威落库', async () => {
    const store = usePipelineMessageStore.getState()
    // 第一次请求挂起（在途）
    let resolveFirst!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve }),
    )
    const firstPromise = store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // 第二次同 id 请求（新快照：seq 1-2）
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '新问题', timestamp: '2026-01-01T00:00:00Z' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '新回复', timestamp: '2026-01-01T00:00:01Z' },
    ])
    const secondPromise = store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    await secondPromise
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)

    // 第一次的响应迟到：promise 静默收场（取消语义，不 reject），陈旧结果不写状态
    resolveFirst({
      data: {
        messages: [
          { id: 'stale-1', sequence: 1, role: 'user', content: '旧问题', timestamp: '2026-01-01T00:00:00Z' },
          { id: 'stale-2', sequence: 2, role: 'assistant', content: '旧回复', timestamp: '2026-01-01T00:00:01Z' },
          { id: 'stale-3', sequence: 3, role: 'assistant', content: '旧回复2', timestamp: '2026-01-01T00:00:02Z' },
        ],
        total: 3,
        has_more: false,
      },
    })
    await expect(firstPromise).resolves.toBeUndefined()
    // 状态仍为第二次请求的权威结果：无陈旧消息、无重复、游标正确
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(2)
    expect(msgs.every((m) => !m.id.startsWith('stale-'))).toBe(true)
    expect(store.getBottomCursor(PIPELINE_ID)).toBe(2)
  })

  it('被取消的向上翻页不残留 loading 标记，翻页请求可继续发起', async () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('u1', 1, { role: 'user', content: 'q' }),
      makeMsg('a1', 2, { content: 'a' }),
    ])

    // 第一次向上翻页挂起
    let resolveFirst!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveFirst = resolve }),
    )
    const firstPromise = store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, before_sequence: 1 })
    expect(usePipelineMessageStore.getState().isLoadingOlderByPipeline[PIPELINE_ID]).toBe(true)

    // 第二次向上翻页取消第一次：loading 标记由新请求重新持有
    setApiRecords(
      [{ id: 'a0', sequence: 0, role: 'assistant', content: 'a0', timestamp: '2026-01-01T00:00:00Z' }],
      false,
    )
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, before_sequence: 1 })
    expect(usePipelineMessageStore.getState().isLoadingOlderByPipeline[PIPELINE_ID]).toBe(false)
    expect(store.getMessages(PIPELINE_ID).map((m) => m.sequence)).toEqual([0, 1, 2])

    // 第一次迟到的翻页结果不写入（不产生重复/乱序）
    resolveFirst({
      data: {
        messages: [
          { id: 'stale-0', sequence: 0, role: 'assistant', content: '旧a0', timestamp: '2026-01-01T00:00:00Z' },
        ],
        total: 1,
        has_more: true,
      },
    })
    await expect(firstPromise).resolves.toBeUndefined()
    expect(store.getMessages(PIPELINE_ID).map((m) => m.sequence)).toEqual([0, 1, 2])
    // has_more 不被迟到结果回滚为 true
    expect(store.hasMoreOlder(PIPELINE_ID)).toBe(false)
  })

  it('不同管道互不取消：A 管道在途请求不受 B 管道新请求影响', async () => {
    const store = usePipelineMessageStore.getState()
    const PIPELINE_B = 'pipe-load-002'
    usePipelineMessageStore.setState({
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID, [PIPELINE_B]: 'thread-load-002' },
    })

    let resolveA!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveA = resolve }),
    )
    const promiseA = store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    setApiRecords([
      { id: 'b1', sequence: 1, role: 'user', content: 'b', timestamp: '2026-01-01T00:00:00Z' },
    ])
    await store.fetchMessages(PIPELINE_B, { threadId: 'thread-load-002' })

    resolveA({
      data: {
        messages: [
          { id: 'a1', sequence: 1, role: 'user', content: 'a', timestamp: '2026-01-01T00:00:00Z' },
        ],
        total: 1,
        has_more: false,
      },
    })
    // A 未被 B 取消：正常写入
    await promiseA
    expect(store.getMessages(PIPELINE_ID).map((m) => m.id)).toEqual(['a1'])
    expect(store.getMessages(PIPELINE_B).map((m) => m.id)).toEqual(['b1'])
  })
})
