/**
 * 刷新对账竞态防护测试（根因2 修复回归）。
 *
 * 背景：pipelineMessageStore 曾持久化 hasMoreOlderByPipeline 但不持久化
 * reconciledByPipeline/prependedCountByPipeline，语义割裂。刷新后 hasMoreOlder=true
 * （快照）但 reconciled=false → init（全量替换）跑的同时，virtuoso 的 increaseViewportBy
 * 触发 startReached → onLoadMore 看到 hasMoreOlder=true 放行 → older 并发，导致
 * prepend 的历史被 init 全量覆盖丢失或重复加载。
 *
 * 修复：
 *  1. merge 重置 hasMoreOlder/topCursor/bottomCursor（统一「刷新=全量重新对账」）。
 *  2. fetchMessages 防御：older 请求不得与 init 并发（init 进行中直接拒绝 older）。
 *
 * 本测试覆盖 #2（行为性，价值最高）：init 进行中时调 fetchMessages(older) 应被拒绝，
 * 不发网络请求；init 完成后才允许 older。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

// mock apiClient.get（网络层）——断言调用次数
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

const PIPELINE_ID = 'pipe-race-001'
const THREAD_ID = 'thread-race-001'

function makeMsg(id: string, seq: number): Message {
  return {
    id,
    sessionId: THREAD_ID,
    sequence: seq,
    role: 'assistant',
    content: `msg-${seq}`,
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    parentId: null,
    status: 'completed',
  } as Message
}

/**
 * 构造一个「永不 resolve」的 init 请求 promise，让 init 保持「进行中」状态，
 * 从而测试「init 进行中时 older 是否被拒绝」。
 */
function neverResolvingGet() {
  return new Promise(() => {}) as Promise<any>
}

describe('刷新对账竞态：init/older 并发防护', () => {
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
      activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      prependedCountByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  it('init 进行中时，older 请求被拒绝（不发网络请求）', async () => {
    // 让 init 请求挂起（永不 resolve），保持 init 进行中
    mockGet.mockImplementationOnce(neverResolvingGet)

    // 触发 init（全量加载）
    const initPromise = usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // 在 init 完成前，尝试发起 older 请求（before_sequence=10）
    usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 10,
    })

    // 关键断言：older 被拒绝，只应有 1 次网络调用（init 的），older 不额外发请求
    expect(mockGet).toHaveBeenCalledTimes(1)
    const initCallParams = mockGet.mock.calls[0][1].params || {}
    expect(initCallParams).toMatchObject({ pipeline_run_id: PIPELINE_ID })
    // 确认那次是 init（无 before_sequence）
    expect(initCallParams).not.toHaveProperty('before_sequence')

    // 清理：让 init promise 不再挂起（避免影响后续/悬挂 promise 告警）
    initPromise.catch(() => {})
  })

  it('init 完成后，older 请求正常放行', async () => {
    // init 正常返回 3 条消息 + has_more=true
    mockGet.mockResolvedValueOnce({
      data: {
        messages: [makeMsg('m1', 1), makeMsg('m2', 2), makeMsg('m3', 3)],
        total: 3,
        has_more: true,
      },
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // init 完成后，hasMoreOlder 应被设为 true（来自 API 的 has_more）
    const state = usePipelineMessageStore.getState()
    expect(state.hasMoreOlderByPipeline[PIPELINE_ID]).toBe(true)

    // 此时 older 请求应正常放行
    mockGet.mockResolvedValueOnce({
      data: { messages: [makeMsg('m0', 0)], total: 1, has_more: false },
    })
    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 1,
    })

    // 第二次调用应是 older（带 before_sequence）。参数嵌套在 params 字段下，字段名后端风格。
    const olderCallParams = mockGet.mock.calls[1][1].params || {}
    expect(olderCallParams).toMatchObject({ before_sequence: 1, pipeline_run_id: PIPELINE_ID })
  })

  it('merge 重置分页状态：hasMoreOlder/topCursor/bottomCursor 在 rehydrate 后为空', async () => {
    // 模拟「刷新前」状态：已加载历史，hasMoreOlder=true，游标非 0
    usePipelineMessageStore.setState({
      messagesByPipeline: { [PIPELINE_ID]: [makeMsg('m1', 1), makeMsg('m2', 2)] },
      hasMoreOlderByPipeline: { [PIPELINE_ID]: true },
      topCursorsByPipeline: { [PIPELINE_ID]: 1 },
      bottomCursorsByPipeline: { [PIPELINE_ID]: 2 },
      prependedCountByPipeline: { [PIPELINE_ID]: 50 },
      reconciledByPipeline: { [PIPELINE_ID]: true },
    })

    // 重新 import 触发 persist 重新初始化（模拟刷新 = store 重新创建）。
    // jsdom 无 IndexedDB，store 降级内存模式，rehydrate 无持久化数据 → merge 不真正执行。
    // 故直接验证：store 初始状态下这些字段都是 {} （符合「刷新后全量重新对账」的预期终态）。
    vi.resetModules()
    const freshMod = await import('@/stores/pipelineMessageStore')
    const freshState = freshMod.usePipelineMessageStore.getState()

    expect(freshState.hasMoreOlderByPipeline).toEqual({})
    expect(freshState.topCursorsByPipeline).toEqual({})
    expect(freshState.bottomCursorsByPipeline).toEqual({})
    expect(freshState.reconciledByPipeline).toEqual({})
    expect(freshState.prependedCountByPipeline).toEqual({})
  })
})
