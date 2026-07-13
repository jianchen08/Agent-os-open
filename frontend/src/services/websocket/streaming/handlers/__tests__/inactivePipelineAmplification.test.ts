/**
 * 非活跃 pipeline 事件放大效应量化测试
 *
 * 验证用户判断："标签页没打开的话就不该推送/不需要处理"。
 *
 * 已通过 e2e 实测确认：后端并发任务时，1000+ 个别人 pipeline 的 chunk/thinking
 * 涌进同一个 WS 连接。当前 handler 无条件为每个事件创建占位符 + 写 store，
 * 即使这些 pipeline 用户根本没开标签页。
 *
 * 本测试模拟该场景，量化：
 *  1. 不过滤时：1004 个别人事件触发多少次 store 写入（放大效应）
 *  2. 加"pipeline 是否被关注"过滤后：写入量应降到 0
 *
 * 关注判据（待实现）：
 *  - 是当前 activePipelineId
 *  - 在 agentTabStore.tabs 里有对应 tab（用户打开了标签页）
 *  - 已在 pipelineStore.pipelines 注册（用户曾交互）
 *
 * 判据：
 *  - 不过滤时 store 写入 ≈ 事件数（每个 chunk 都写）
 *  - 过滤后 store 写入 = 0（别人 pipeline 的事件全丢弃）
 *  - 这证明"前端过滤非活跃 pipeline"能消除放大效应
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

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

const MY_PIPELINE = 'pipe-mine-active-001'      // 用户当前活跃的管道
const OTHER_PIPELINE_1 = 'pipe-other-aaa-001'    // 别人的管道1
const OTHER_PIPELINE_2 = 'pipe-other-bbb-002'    // 别人的管道2
const MESSAGE_ID = 'msg-streaming-001'

function makeEvent(eventType: string, pipelineId: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: { pipeline_id: pipelineId, message_id: MESSAGE_ID, ...data },
    source_type: 'system',
    source_id: pipelineId,
    timestamp: new Date().toISOString(),
  }
}

describe('非活跃 pipeline 事件放大效应', () => {
  let usePipelineMessageStore: any
  let handleStreamChunk: any
  let handleStreamStart: any

  beforeEach(async () => {
    vi.useFakeTimers()
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: {},
      streamingState: {}, activePipelineId: MY_PIPELINE,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    // 只注册【我的】管道，模拟"我只开了这一个标签页"
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: MY_PIPELINE, sessionId: 'session-mine', level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamChunk = handlerMod.handleStreamChunk
    handleStreamStart = handlerMod.handleStreamStart
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  /** 统计 store 中所有 pipeline 的消息总数 */
  function totalMessagesInStore(): number {
    const state = usePipelineMessageStore.getState()
    return Object.values(state.messagesByPipeline).reduce(
      (sum: number, msgs: any) => sum + (msgs?.length || 0), 0,
    )
  }

  /** 统计 store 里有多少个不同的 pipeline（被自动注册的"幽灵管道"） */
  function ghostPipelineCount(): number {
    const state = usePipelineMessageStore.getState()
    return Object.keys(state.messagesByPipeline).filter(
      (pid) => pid !== MY_PIPELINE,
    ).length
  }

  it('过滤生效：别人 pipeline 的 chunk/start 不创建 store 条目', async () => {
    // 模拟 e2e 实测场景：2 个别人管道各发 500 个 chunk + stream_start
    handleStreamStart(makeEvent('stream_start', OTHER_PIPELINE_1, {}))
    handleStreamStart(makeEvent('stream_start', OTHER_PIPELINE_2, {}))

    for (let i = 0; i < 500; i++) {
      handleStreamChunk(makeEvent('stream_chunk', OTHER_PIPELINE_1, { content: '甲', sequence: 1 }))
      handleStreamChunk(makeEvent('stream_chunk', OTHER_PIPELINE_2, { content: '乙', sequence: 1 }))
    }
    await vi.advanceTimersByTimeAsync(16)

    const ghostPipelines = ghostPipelineCount()
    const totalMsgs = totalMessagesInStore()
    console.log(`[过滤后] 别人发了 1004 个事件后:`)
    console.log(`  幽灵管道数: ${ghostPipelines} (期望 0)`)
    console.log(`  store 总消息数: ${totalMsgs} (期望 0)`)
    console.log(`  我的活跃管道消息数: ${usePipelineMessageStore.getState().messagesByPipeline[MY_PIPELINE]?.length || 0}`)

    // 关键断言：非活跃 pipeline 的事件被全部丢弃，不创建幽灵管道
    expect(ghostPipelines).toBe(0)
    expect(totalMsgs).toBe(0)
  })

  it('对照：我自己的活跃 pipeline 事件正常处理', async () => {
    handleStreamStart(makeEvent('stream_start', MY_PIPELINE, {}))
    for (let i = 0; i < 10; i++) {
      handleStreamChunk(makeEvent('stream_chunk', MY_PIPELINE, { content: '我', sequence: 1 }))
    }
    await vi.advanceTimersByTimeAsync(16)

    const myMsgs = usePipelineMessageStore.getState().messagesByPipeline[MY_PIPELINE]?.length || 0
    console.log(`[我的管道] 10 个 chunk 后消息数: ${myMsgs} (期望 >=1)`)
    // 我的活跃管道事件必须正常处理
    expect(myMsgs).toBeGreaterThanOrEqual(1)
    // 幽灵管道仍应为 0
    expect(ghostPipelineCount()).toBe(0)
  })

  it('已注册但非活跃的 pipeline（如会话切换预注册）事件也处理', async () => {
    // 模拟：用户在会话A，但会话B的管道已注册（之前交互过）
    const REGISTERED_OTHER = 'pipe-registered-other'
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: REGISTERED_OTHER, sessionId: 's', level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })
    handleStreamStart(makeEvent('stream_start', REGISTERED_OTHER, {}))
    handleStreamChunk(makeEvent('stream_chunk', REGISTERED_OTHER, { content: 'x', sequence: 1 }))
    await vi.advanceTimersByTimeAsync(16)

    const msgs = usePipelineMessageStore.getState().messagesByPipeline[REGISTERED_OTHER]?.length || 0
    console.log(`[已注册管道] 消息数: ${msgs} (期望 >=1，已注册=用户关注)`)
    expect(msgs).toBeGreaterThanOrEqual(1)
  })
})
