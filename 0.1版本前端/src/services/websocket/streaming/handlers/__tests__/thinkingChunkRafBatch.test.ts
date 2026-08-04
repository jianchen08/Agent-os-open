/**
 * 回归测试：thinking_chunk 走 RAF 批处理（与 stream_chunk 一致）
 *
 * 修复前：handleThinkingChunk 同步调 appendToPart，每个 chunk 立即触发 store 更新 →
 * React 重渲染阻塞主线程 → 思考"匀速逐字慢"且正文 chunk 积压等主线程空闲才一次性 flush。
 * 修复后：thinking_chunk 进 bufferChunk('thinking')，由 RAF 统一刷写。
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

const PIPELINE_ID = 'pipe-thinking-raf-001'
const MESSAGE_ID = 'msg_thinking_raf_01'
const THREAD_ID = 'thread-thinking-raf-001'

function makeEvent(eventType: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: { pipeline_id: PIPELINE_ID, message_id: MESSAGE_ID, ...data },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

function snapshotThinking() {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg) return { found: false, content: '', state: '' }
  const tp = (msg.parts || []).find((p: any) => p.type === 'thinking')
  return { found: true, content: tp?.content || '', state: tp?.state || '' }
}

describe('thinking_chunk RAF 批处理', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
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
      streamingState: {}, activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    } as any)

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = handlerMod.handleStreamStart
    handleThinkingStart = handlerMod.handleThinkingStart
    handleThinkingChunk = handlerMod.handleThinkingChunk
    handleThinkingEnd = handlerMod.handleThinkingEnd

    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('场景1：thinking_chunk 逐帧累积（RAF 批处理生效）', async () => {
    handleThinkingStart(makeEvent('thinking_start', {}))

    const chunks = ['让', '我', '想', '想']
    const seen: any[] = []
    for (const chunk of chunks) {
      handleThinkingChunk(makeEvent('thinking_chunk', { content: chunk }))
      await vi.advanceTimersByTimeAsync(16)
      seen.push(snapshotThinking())
    }

    // 每个 chunk 后推进一帧，thinking content 应逐帧累积
    expect(seen[0].content).toBe('让')
    expect(seen[1].content).toBe('让我')
    expect(seen[2].content).toBe('让我想')
    expect(seen[3].content).toBe('让我想想')
  })

  it('场景2：未推进 RAF 时 thinking chunk 在 buffer，不立即写入', () => {
    handleThinkingStart(makeEvent('thinking_start', {}))

    // 连续发 chunk 不推进 RAF
    for (const chunk of ['让', '我', '想', '想']) {
      handleThinkingChunk(makeEvent('thinking_chunk', { content: chunk }))
    }
    const beforeFlush = snapshotThinking()
    // chunk 还在 buffer，thinking part 内容为空（start 创建的空 part）
    expect(beforeFlush.content).toBe('')

    // thinking_end 会 flush buffer
    handleThinkingEnd(makeEvent('thinking_end', {}))
    const afterEnd = snapshotThinking()
    // flush 后内容一次性出现（end 触发 flushStreamChunkBuffer）
    expect(afterEnd.content).toBe('让我想想')
    expect(afterEnd.state).toBe('done')
  })

  it('场景3：thinking_end 前 flush 不丢末尾内容', async () => {
    handleThinkingStart(makeEvent('thinking_start', {}))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '思考内容' }))
    // 不推进 RAF，直接 end
    handleThinkingEnd(makeEvent('thinking_end', {}))

    const snap = snapshotThinking()
    expect(snap.content).toBe('思考内容')
    expect(snap.state).toBe('done')
  })
})
