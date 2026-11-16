/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * F1 回归：刷新后 streamingState 被 persist merge 重置为 {}，
 * handleReconnected 仍应据 messagesByPipeline 里 status==='streaming' 的消息
 * 对相应管道（含子管道）执行 backfill，避免刷新后子管道消息静默丢失。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

vi.mock('@/stores/contextUsageStore', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).contextUsageMock())

vi.mock('@/stores/notificationStore', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).notificationStoreMock())

const SUB_PID = 'pipe-sub-001'
const MAIN_PID = 'pipe-main-001'
const SID = 'sess-1'

describe('handleReconnected 刷新后 backfill 覆盖子管道 (F1)', () => {
  let usePipelineMessageStore: any
  let handleReconnected: any
  let loadCalls: string[]

  beforeEach(async () => {
    vi.resetModules()
    loadCalls = []
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    const ts = new Date().toISOString()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        // 子管道有一条还在 streaming 的 assistant 消息（刷新后从 IndexedDB 恢复）
        [SUB_PID]: [
          {
            id: 'm1',
            role: 'assistant',
            status: 'streaming',
            content: '',
            parts: [],
            sequence: 5,
            timestamp: ts,
          },
        ],
        // 主管道无 streaming 消息
        [MAIN_PID]: [
          { id: 'm2', role: 'assistant', status: 'completed', content: 'hi', parts: [], sequence: 1, timestamp: ts },
        ],
      },
      pipelineSessionMap: { [SUB_PID]: SID, [MAIN_PID]: SID },
      streamingState: {}, // ★ 刷新后为空——F1 bug 的触发条件
      pipelines: {},
      activePipelineId: MAIN_PID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
    // 用 spy 替换 loadPipelineMessages，记录 backfill 目标管道
    usePipelineMessageStore.setState({
      loadPipelineMessages: vi.fn(async (pid: string) => {
        loadCalls.push(pid)
        return { ok: true }
      }),
    })

    const mod = await import('@/services/websocket/streaming/lifecycleHandlers')
    handleReconnected = mod.handleReconnected
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('streamingState 为空、但有 streaming 消息时，仍对该子管道 backfill', async () => {
    await handleReconnected()
    expect(loadCalls).toContain(SUB_PID)
  })

  it('无 streaming 消息的管道不被多余 backfill', async () => {
    await handleReconnected()
    expect(loadCalls).not.toContain(MAIN_PID)
  })

  it('无 key 终态消息（completed/interrupted 无 seq）的管道进补漏集合（终态必携排序键，重连即对账）', async () => {
    // 停止/出错与 WS 断窗重叠时，本地终态化先于 stream_end 到达且事件丢失——
    // 无 seq 终态消息按排序键（缺 seq 恒排末尾）永久垫底，重连是最后对账点。
    const ts = new Date().toISOString()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        [SUB_PID]: [
          {
            id: 'a_stopped',
            role: 'assistant',
            status: 'interrupted',
            content: '半截回复',
            parts: [],
            sequence: null,
            timestamp: ts,
          },
        ],
        [MAIN_PID]: [
          { id: 'm2', role: 'assistant', status: 'completed', content: 'hi', parts: [], sequence: 1, timestamp: ts },
        ],
      },
      streamingState: {},
    })
    await handleReconnected()
    expect(loadCalls).toContain(SUB_PID)
    expect(loadCalls).not.toContain(MAIN_PID)
  })

  it('非终态无 seq（sending 乐观气泡）不触发补漏（pending 态不进 keyed 排序域）', async () => {
    const ts = new Date().toISOString()
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        [SUB_PID]: [
          {
            id: 'u_sending',
            role: 'user',
            status: 'sending',
            content: '发送中',
            parts: [],
            sequence: null,
            timestamp: ts,
          },
        ],
      },
      streamingState: {},
    })
    await handleReconnected()
    expect(loadCalls).not.toContain(SUB_PID)
  })

  it('F2: backfill 后仍 streaming 的消息——宽限期零活动后标记 interrupted + warning part', async () => {
    // loadPipelineMessages mock 不更新消息（模拟后端无记录、未能恢复）→ 仍 streaming。
    // 2026-08-23 宽限重查：重连瞬间不标（活流误判防护），20s 零活动后才标真·丢失。
    vi.useFakeTimers()
    try {
      await handleReconnected()
      // 宽限期内不立即标中断
      let msg = usePipelineMessageStore.getState().messagesByPipeline[SUB_PID][0]
      expect(msg.status).toBe('streaming')
      await vi.advanceTimersByTimeAsync(20_000)
      msg = usePipelineMessageStore.getState().messagesByPipeline[SUB_PID][0]
      expect(msg.status).toBe('interrupted')
      const sysPart = (msg.parts || []).find(
        (p: any) => p.type === 'system' && p.notificationType === 'stream_interrupted',
      )
      expect(sysPart, '应追加 stream_interrupted warning part').toBeTruthy()
      expect(sysPart.level).toBe('warning')
    } finally {
      vi.useRealTimers()
    }
  })
})
