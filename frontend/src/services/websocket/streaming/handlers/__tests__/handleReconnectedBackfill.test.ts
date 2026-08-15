/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * F1 回归：刷新后 streamingState 被 persist merge 重置为 {}，
 * handleReconnected 仍应据 messagesByPipeline 里 status==='streaming' 的消息
 * 对相应管道（含子管道）执行 backfill，避免刷新后子管道消息静默丢失。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: { getState: () => ({ clear: vi.fn(), set: vi.fn(), get: () => null }) },
}))

vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: vi.fn() }) },
}))

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

  it('F2: backfill 后仍 streaming 的消息标记 interrupted + warning part', async () => {
    // loadPipelineMessages mock 不更新消息（模拟后端无记录、未能恢复）→ 仍 streaming
    await handleReconnected()
    const msg = usePipelineMessageStore.getState().messagesByPipeline[SUB_PID][0]
    expect(msg.status).toBe('interrupted')
    const sysPart = (msg.parts || []).find(
      (p: any) => p.type === 'system' && p.notificationType === 'stream_interrupted',
    )
    expect(sysPart, '应追加 stream_interrupted warning part').toBeTruthy()
    expect(sysPart.level).toBe('warning')
  })
})
