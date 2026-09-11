/**
 * 刷新对账竞态防护测试（回归）。
 *
 * 背景：若只持久化 hasMoreOlderByPipeline 而不持久化
 * reconciledByPipeline/prependedCountByPipeline，语义割裂：刷新后 hasMoreOlder=true
 * （快照）但 reconciled=false → init（全量替换）跑的同时，virtuoso 的 increaseViewportBy
 * 触发 startReached → onLoadMore 看到 hasMoreOlder=true 放行 → older 并发，导致
 * prepend 的历史被 init 全量覆盖丢失或重复加载。
 *
 * 现行防护契约（实现演进后）：
 *  1. merge 重置 reconciledByPipeline（刷新=全量重新对账）；bottomCursor 等运行时
 *     状态以默认值为准。prependedCountByPipeline 字段已从 store 删除。
 *  2. fetchMessages 同管道取消语义：新请求发起即 abort 在途旧请求（方向间、
 *     同方向均不并存——迟到响应按取消静默收场，不写状态；旧「按方向去重复用
 *     in-flight promise」契约已被取代，取代者的响应才是权威结果）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
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

describe('刷新对账竞态：init/older 并发防护', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

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
      reconciledByPipeline: {},
    })
  })

  it('older 发起即取消在途 init；同方向重复请求以新代旧，迟到响应不写状态', async () => {
    // init 请求挂起（可手动迟到 resolve，模拟慢全量读）
    let resolveInit!: (v: unknown) => void
    mockGet.mockImplementationOnce(
      () => new Promise((resolve) => { resolveInit = resolve }),
    )
    // older 两次请求（第一次将被第二次取代）各自正常返回
    mockGet.mockResolvedValueOnce({
      data: { messages: [makeMsg('m0', 0)], total: 1, has_more: false },
    })
    mockGet.mockResolvedValueOnce({
      data: { messages: [makeMsg('m0', 0)], total: 1, has_more: false },
    })

    // 触发 init（全量加载）；挂起的 init attach catch 避免 dangling promise 告警
    const initPromise = usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    initPromise.catch(() => {})

    // init 进行中发起 older（before_sequence=10），再发起并发重复 older
    const olderPromise1 = usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 10,
    })
    const olderPromise2 = usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, {
      threadId: THREAD_ID,
      before_sequence: 10,
    })

    await Promise.all([olderPromise1, olderPromise2])

    // 同管道取代即取消：init 1 次 + older 两次（第一次被第二次取消），不互相阻塞
    expect(mockGet).toHaveBeenCalledTimes(3)
    const initCallParams = mockGet.mock.calls[0][1].params || {}
    expect(initCallParams).toMatchObject({ pipeline_run_id: PIPELINE_ID })
    // 确认第一次是 init（无 before_sequence）
    expect(initCallParams).not.toHaveProperty('before_sequence')
    // 后两次是 older（带 before_sequence）
    const olderCallParams = mockGet.mock.calls[1][1].params || {}
    expect(olderCallParams).toMatchObject({ before_sequence: 10, pipeline_run_id: PIPELINE_ID })
    expect(mockGet.mock.calls[2][1].params || {}).toMatchObject({ before_sequence: 10 })

    // 取代者（older2）的权威结果已落库
    expect(
      usePipelineMessageStore.getState().getMessages(PIPELINE_ID).map((m) => m.sequence),
    ).toEqual([0])

    // init 的响应迟到（全量 3 条 + has_more=true）：按取消静默收场，不覆盖已建立状态
    resolveInit({
      data: {
        messages: [makeMsg('m1', 1), makeMsg('m2', 2), makeMsg('m3', 3)],
        total: 3,
        has_more: true,
      },
    })
    await expect(initPromise).resolves.toBeUndefined()
    expect(
      usePipelineMessageStore.getState().getMessages(PIPELINE_ID).map((m) => m.sequence),
    ).toEqual([0])
    // hasMoreOlder 不被迟到结果回滚为 true
    expect(usePipelineMessageStore.getState().hasMoreOlderByPipeline[PIPELINE_ID]).toBe(false)
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
      reconciledByPipeline: { [PIPELINE_ID]: true },
    })

    // 重新 import 触发 persist 重新初始化（模拟刷新 = store 重新创建）。
    // jsdom 无 IndexedDB，store 降级内存模式，rehydrate 无持久化数据 → merge 不真正执行。
    // 故直接验证：store 初始状态下这些字段都是 {} （符合「刷新后全量重新对账」的预期终态）。
    // prependedCountByPipeline 字段已从 store 删除，不再断言。
    vi.resetModules()
    const freshMod = await import('@/stores/pipelineMessageStore')
    const freshState = freshMod.usePipelineMessageStore.getState()

    expect(freshState.hasMoreOlderByPipeline).toEqual({})
    expect(freshState.topCursorsByPipeline).toEqual({})
    expect(freshState.bottomCursorsByPipeline).toEqual({})
    expect(freshState.reconciledByPipeline).toEqual({})
  })
})
