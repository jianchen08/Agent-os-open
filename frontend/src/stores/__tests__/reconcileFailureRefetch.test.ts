// @feature FP-T12 前端组件补测
/**
 * 对账失败处理测试（C6）：后台全量对账失败不再静默——缓存与 DB 的分叉
 * （空洞/残影）无人修正会永久留存。契约：
 * - 对账失败 → warn 显式记录 + 借既有 fetchMessages 全量强制重拉一次；
 * - 重拉成功 → initFromAPI 权威替换 + 标记已对账；
 * - 重拉仍失败 → 保留缓存现状、不误标已对账（待下次进入/WS 重连重试）；
 * - 一次通过对账 → 仅拉一次，不额外重拉（回归）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'

const { mockGetMessages, sessionWarn } = vi.hoisted(() => ({
  mockGetMessages: vi.fn(),
  sessionWarn: vi.fn(),
}))

vi.mock('@/services/api/session', () => ({
  getMessages: (...args: unknown[]) => mockGetMessages(...args),
  mergeConsecutiveAssistantMessages: (msgs: unknown[]) => msgs,
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: sessionWarn,
      error: vi.fn(),
    },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

const PIPELINE_ID = 'pipe-reconcile-1'
const THREAD_ID = 'thread-reconcile-1'

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

describe('后台全量对账失败 → warn + 强制重拉一次', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    // 本地有缓存（存量消息）+ 未对账 → auto 进入触发后台对账
    usePipelineMessageStore.setState({
      messagesByPipeline: { [PIPELINE_ID]: [makeMsg('msg-cached', 1)] },
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

  it('对账失败 → warn 后强制重拉一次；重拉成功以权威数据替换并标记已对账', async () => {
    mockGetMessages
      .mockRejectedValueOnce(new Error('网络抖动'))
      .mockResolvedValueOnce({ messages: [makeMsg('msg-authority', 1), makeMsg('msg-authority', 2)], has_more: false })

    // auto 进入：本地缓存渲染（秒开），对账放后台（不阻塞返回）
    await usePipelineMessageStore.getState().loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    await vi.waitFor(() => {
      expect(mockGetMessages).toHaveBeenCalledTimes(2)
    })
    // 重拉命中同一管道（全量模式，非增量游标）
    expect(mockGetMessages).toHaveBeenLastCalledWith(
      THREAD_ID,
      expect.objectContaining({ pipelineRunId: PIPELINE_ID }),
    )
    // 权威数据替换缓存 + 标记已对账
    const msgs = usePipelineMessageStore.getState().messagesByPipeline[PIPELINE_ID] ?? []
    expect(msgs.map((m) => m.id)).toEqual(['msg-authority', 'msg-authority'])
    expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBe(true)
    // 对账失败显式 warn（含管道标识），不再是静默 catch
    expect(sessionWarn).toHaveBeenCalledWith(
      expect.stringContaining('全量对账失败'),
      PIPELINE_ID,
      expect.anything(),
    )
  })

  it('对账与重拉均失败 → 缓存现状保留、不误标已对账', async () => {
    mockGetMessages.mockRejectedValue(new Error('持续故障'))

    await usePipelineMessageStore.getState().loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    await vi.waitFor(() => {
      expect(mockGetMessages).toHaveBeenCalledTimes(2)
    })
    // 缓存消息原封保留（现状不动，等待下次触发）
    const msgs = usePipelineMessageStore.getState().messagesByPipeline[PIPELINE_ID] ?? []
    expect(msgs.map((m) => m.id)).toEqual(['msg-cached'])
    // 不误标已对账（下次 auto 进入仍会尝试对账）
    expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBeUndefined()
    // 失败有显式 warn：每次对账入口一条（含管道标识与错误摘要）；重拉失败由
    // fetchMessages 自带失败日志承载，不重复刷屏
    const reconcileWarns = sessionWarn.mock.calls.filter((args) =>
      String(args[0]).includes('全量对账失败'),
    )
    expect(reconcileWarns).toHaveLength(1)
    expect(reconcileWarns[0]).toEqual([
      expect.stringContaining('全量对账失败'),
      PIPELINE_ID,
      expect.anything(),
    ])
  })

  it('对账一次通过 → 仅拉一次，无额外重拉（回归）', async () => {
    mockGetMessages.mockResolvedValue({ messages: [makeMsg('msg-ok', 1)], has_more: false })

    await usePipelineMessageStore.getState().loadPipelineMessages(PIPELINE_ID, { threadId: THREAD_ID })

    await vi.waitFor(() => {
      expect(usePipelineMessageStore.getState().reconciledByPipeline[PIPELINE_ID]).toBe(true)
    })
    expect(mockGetMessages).toHaveBeenCalledTimes(1)
    expect(sessionWarn).not.toHaveBeenCalled()
  })
})
