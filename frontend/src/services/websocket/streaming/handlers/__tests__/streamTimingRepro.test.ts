/**
 * 流式渲染时序端到端复现测试
 *
 * 复现用户报告："转圈4-5秒，然后一下子全部弹出来"。
 *
 * 测试驱动真实 WS handler（handleStreamStart/Chunk/End + handleThinkingStart/Chunk/End）
 * + 真实 pipelineMessageStore + 真实 RAF 批处理（_flushChunks / _scheduleFlush），
 * 不 mock 任何流式状态管理逻辑。
 *
 * 三个递进场景：
 *  1. 基线：每 chunk 后推进 RAF → store 应逐帧累积内容（"正确路径"长什么样）
 *  2. 复现：连续发 chunk 但不推进 RAF（镜像"主线程被阻塞/RAF 被推迟"），
 *          直接发 stream_end → store 表现为"前面一直空，结束时一次性填满"
 *  3. 状态清理：stream_end 后残留 state='streaming' 的 thinking part 是否被兜底清理
 *
 * 判据：
 *  - 场景1 能逐帧累积 → 说明 RAF 批处理本身正确，"一次弹出"不是它的问题
 *  - 场景2 能复现"一次弹出" → 定位到"chunk 缓冲未被逐帧 flush"，根因是 RAF 被推迟
 *  - 场景3 残留 streaming part → 确认 stream_end 清理路径不闭环（独立 bug）
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

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

const PIPELINE_ID = 'pipe-stream-timing-001'
const MESSAGE_ID = 'msg_stream_timing_01'
const THREAD_ID = 'thread-stream-timing-001'

/** 构造后端 WS 事件信封（与 bridge_core._make_event 一致：业务字段在 data 下） */
function makeEvent(eventType: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      ...data,
    },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

/** 取目标消息及其 parts 的可读快照 */
function snapshotMessage() {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg) return { found: false, content: '', textPartStates: [], thinkingPartStates: [], status: '' }
  const textParts = (msg.parts || []).filter((p: any) => p.type === 'text')
  const thinkingParts = (msg.parts || []).filter((p: any) => p.type === 'thinking')
  return {
    found: true,
    content: msg.content || '',
    status: msg.status,
    textContent: textParts.map((p: any) => p.content || '').join(''),
    textPartStates: textParts.map((p: any) => ({ state: p.state, len: (p.content || '').length })),
    thinkingPartStates: thinkingParts.map((p: any) => ({ state: p.state, len: (p.content || '').length })),
  }
}

describe('流式渲染时序：复现"转圈后一次性全部弹出"', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
  let handleStreamChunk: any
  let handleStreamEnd: any
  let handleThinkingStart: any
  let handleThinkingChunk: any
  let handleThinkingEnd: any

  beforeEach(async () => {
    vi.useFakeTimers()
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    ;(window as any).__pipelineStore = usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {}, activePipelineId: null,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = handlerMod.handleStreamStart
    handleStreamChunk = handlerMod.handleStreamChunk
    handleStreamEnd = handlerMod.handleStreamEnd
    handleThinkingStart = handlerMod.handleThinkingStart
    handleThinkingChunk = handlerMod.handleThinkingChunk
    handleThinkingEnd = handlerMod.handleThinkingEnd
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('场景1（基线）：每收到一个 chunk 就推进 RAF，store 应逐帧累积内容', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    const chunks = ['你', '好', '世', '界']
    const seenAtEachChunk: any[] = []

    for (const chunk of chunks) {
      handleStreamChunk(makeEvent('stream_chunk', { content: chunk, sequence: 2 }))
      // 每收到一个 chunk，推进一帧（16ms），让 _flushChunks 执行
      await vi.advanceTimersByTimeAsync(16)
      seenAtEachChunk.push(snapshotMessage())
    }

    console.log('[场景1] 每个 chunk 后的 store 快照:', JSON.stringify(seenAtEachChunk, null, 2))

    // 断言：内容逐帧累积，每帧比前一帧长 1 个字
    expect(seenAtEachChunk[0].textContent).toBe('你')
    expect(seenAtEachChunk[1].textContent).toBe('你好')
    expect(seenAtEachChunk[2].textContent).toBe('你好世')
    expect(seenAtEachChunk[3].textContent).toBe('你好世界')
  })

  it('场景2（复现）：连续发 chunk 但不推进 RAF，stream_end 前 store 一直为空占位符', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 记录 stream_start 后、任何 chunk flush 前的状态
    const beforeAnyChunk = snapshotMessage()
    console.log('[场景2] stream_start 后状态:', JSON.stringify(beforeAnyChunk))

    // 连续发 4 个 chunk，但【不推进 RAF】（镜像主线程被阻塞/RAF 被推迟）
    for (const chunk of ['你', '好', '世', '界']) {
      handleStreamChunk(makeEvent('stream_chunk', { content: chunk, sequence: 2 }))
    }
    // 不推进 timer，检查 store —— 此时 chunk 应仍在 RAF buffer 里，未写入
    const afterChunksNoFlush = snapshotMessage()
    console.log('[场景2] 4 chunk 后（未推进 RAF）状态:', JSON.stringify(afterChunksNoFlush, null, 2))

    // 关键断言 A：未推进 RAF 时，store 里文本 part 内容为空（chunk 还在 buffer）
    expect(afterChunksNoFlush.textContent).toBe('')

    // 现在发 stream_end（其内部会同步调 flushStreamChunkBuffer 一次性吐出全部）
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '你好世界',
      final_sequence: 5,
      parts: [
        { type: 'text', content: '你好世界', state: 'done', sequence: 2 },
      ],
    }))
    const afterStreamEnd = snapshotMessage()
    console.log('[场景2] stream_end 后状态:', JSON.stringify(afterStreamEnd, null, 2))

    // 关键断言 B：stream_end 后内容一次性出现（"一次全部弹出"）
    expect(afterStreamEnd.textContent).toBe('你好世界')
    // 关键断言 C：这就是"转圈后一次弹出"的特征——前面一直空，结尾一次填满
    const wentFromEmptyToFull = afterChunksNoFlush.textContent === '' && afterStreamEnd.textContent === '你好世界'
    console.log('[场景2] 是否复现"转圈后一次弹出":', wentFromEmptyToFull)
    expect(wentFromEmptyToFull).toBe(true)
  })

  it('场景3（状态清理）：thinking part 残留 state=streaming 时，stream_end 是否兜底清理', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 思考开始 + chunk（state 被设为 'streaming'）
    handleThinkingStart(makeEvent('thinking_start', { sequence: 1 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '我在思考...', sequence: 1 }))
    // 【故意不发 thinking_end】模拟 thinking_end 丢失/乱序，part 残留 state='streaming'

    const beforeStreamEnd = snapshotMessage()
    console.log('[场景3] stream_end 前 thinking part 状态:', JSON.stringify(beforeStreamEnd.thinkingPartStates))

    // 正文 chunk
    handleStreamChunk(makeEvent('stream_chunk', { content: '回复', sequence: 3 }))
    await vi.advanceTimersByTimeAsync(16)

    // 发 stream_end
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '回复',
      final_sequence: 5,
      parts: [
        { type: 'thinking', content: '我在思考...', state: 'done', sequence: 1 },
        { type: 'text', content: '回复', state: 'done', sequence: 3 },
      ],
    }))
    const afterStreamEnd = snapshotMessage()
    console.log('[场景3] stream_end 后 thinking part 状态:', JSON.stringify(afterStreamEnd.thinkingPartStates))
    console.log('[场景3] stream_end 后消息 status:', afterStreamEnd.status)

    // 关键断言 D：stream_end 后所有 thinking part 的 state 应为 'done'（不再转圈）
    const hasStreamingThinking = afterStreamEnd.thinkingPartStates.some((p: any) => p.state === 'streaming')
    console.log('[场景3] stream_end 后是否仍有 state=streaming 的 thinking part:', hasStreamingThinking)
    // 如果这个断言【失败】，说明 stream_end 不清理 thinking part state ——
    // 即"思考图标一直转"的根因，需要修复。
    expect(hasStreamingThinking).toBe(false)
  })
})
