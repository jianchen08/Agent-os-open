/** @feature 执行态真值源 | @ci: frontend-test */
/**
 * run 级收尾事件与生成态生命周期（2026-08-31）
 *
 * 引擎逐轮发射 stream_end（一轮 = 一条消息），工具执行轮间若以 stream_end
 * 终止生成态，会把「执行中」误判回「空闲」——busy 发送分支失效，乐观气泡
 * 与待发队列同屏（互斥破坏）。修复后的契约：
 * - stream_end 只收尾当轮消息，不得终止 streamingState
 * - pipeline_round_finished 是生成态的唯一 run 级终止信号（failed 标志驱动注册表状态）
 * - 90s 轮次兜底只掐「消息仍 streaming」的真断流，不掐轮间生成态
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

const applyStreamStatusMock = vi.fn()
vi.mock('@/stores/pipelineRegistryStore', () => ({
  usePipelineRegistryStore: {
    getState: () => ({ applyStreamStatus: applyStreamStatusMock }),
  },
}))

const PIPELINE_ID = 'pipe-round-finished-001'
const MESSAGE_ID = 'msg_round_finished_01'
const MESSAGE_ID_R2 = 'msg_round_finished_02'
const THREAD_ID = 'thread-round-finished-001'

function makeEvent(eventType: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: {
      pipeline_id: PIPELINE_ID,
      _threadId: THREAD_ID,
      ...data,
    },
  }
}

describe('生成态生命周期：stream_end 轮级收尾 vs pipeline_round_finished run 级终止', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
  let handleStreamEnd: any
  let handlePipelineRoundFinished: any

  beforeEach(async () => {
    vi.useFakeTimers()
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    ;(window as any).__pipelineStore = usePipelineMessageStore
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
    })
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: THREAD_ID,
      level: 1,
      tabId: null,
      agentName: '',
      status: 'idle',
      parentId: null,
      unreadCount: 0,
    })
    const mod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = mod.handleStreamStart
    handleStreamEnd = mod.handleStreamEnd
    handlePipelineRoundFinished = mod.handlePipelineRoundFinished
    applyStreamStatusMock.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (window as any).__pipelineStore
  })

  const isStreaming = () =>
    usePipelineMessageStore.getState().streamingState[PIPELINE_ID]?.isStreaming ?? false

  it('工具轮间形态：stream_end 后生成态必须维持（轮收尾不终止执行态）', () => {
    // 第一轮：start → end（assistant 带 tool_call 收尾，工具开始执行）
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    expect(isStreaming()).toBe(true)
    handleStreamEnd(
      makeEvent('stream_end', { message_id: MESSAGE_ID, final_sequence: 1 }),
    )
    // 轮间（工具执行数分钟）：生成态不得被 stream_end 掐断
    expect(isStreaming()).toBe(true)
    // 第二轮工具结果回来：新 message_id 的 stream_start 正常接管
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID_R2 }))
    expect(isStreaming()).toBe(true)
  })

  it('pipeline_round_finished(failed=false)：终止生成态并标注册表 completed', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    expect(isStreaming()).toBe(true)

    handlePipelineRoundFinished(
      makeEvent('pipeline_round_finished', { failed: false }),
    )
    expect(isStreaming()).toBe(false)
    expect(applyStreamStatusMock).toHaveBeenCalledWith(PIPELINE_ID, 'completed')
  })

  it('pipeline_round_finished(failed=true)：注册表标 failed（失败路径双终止幂等）', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    handlePipelineRoundFinished(makeEvent('pipeline_round_finished', { failed: true }))
    expect(isStreaming()).toBe(false)
    expect(applyStreamStatusMock).toHaveBeenCalledWith(PIPELINE_ID, 'failed')
  })

  it('pipeline_id 缺失的收尾事件不误清任何管道', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    applyStreamStatusMock.mockClear()
    const ev = { type: 'pipeline_round_finished', data: { failed: false } }
    handlePipelineRoundFinished(ev)
    expect(isStreaming()).toBe(true)
    expect(applyStreamStatusMock).not.toHaveBeenCalled()
  })

  it('90s 轮次兜底豁免：消息已随轮收尾（completed）时推进 90s 不掐生成态', async () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    // 轮收尾：消息 completed（stream_end 消息收尾路径），工具开始执行
    handleStreamEnd(
      makeEvent('stream_end', { message_id: MESSAGE_ID, final_sequence: 1 }),
    )
    usePipelineMessageStore.getState().finalizeMessage(PIPELINE_ID, MESSAGE_ID)
    expect(isStreaming()).toBe(true)

    await vi.advanceTimersByTimeAsync(90_000)
    // run 仍在跑：轮间生成态不得被 90s 兜底掐断
    expect(isStreaming()).toBe(true)
  })

  it('90s 轮次兜底保留：消息仍 streaming（chunk 断流真卡死）时推进 90s 强制收尾', async () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MESSAGE_ID }))
    // 不发 stream_end（断流）：消息滞留 streaming
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.some((m: any) => m.id === MESSAGE_ID && m.status === 'streaming')).toBe(true)

    await vi.advanceTimersByTimeAsync(90_000)
    expect(isStreaming()).toBe(false)
  })
})
