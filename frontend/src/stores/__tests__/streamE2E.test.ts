/**
 * 端到端测试：验证 stream_start → stream_end 流程中消息不丢失
 *
 * 直接调用 handleStreamStart / handleStreamEnd 的真实逻辑，
 * 不 mock pipelineStore，验证消息在 store 中的完整生命周期
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
  // applyServerPayload 的权威合并入口（以完整形态生成 parts）
  mapBackendMessageToMessage: (msg: any, threadId: string) => ({
    id: msg.id,
    role: msg.role || 'assistant',
    content: msg.content || '',
    sequence: msg.sequence,
    status: msg.status || 'completed',
    timestamp: msg.timestamp || new Date().toISOString(),
    threadId,
    parentId: null,
    parts: msg.content
      ? [{ type: 'text', content: msg.content, state: 'done' }]
      : [],
  }),
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

describe('stream 端到端：handleStreamStart → handleStreamEnd', () => {
  let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let handleStreamStart: typeof import('@/services/websocket/streaming/handlers').handleStreamStart
  let handleStreamEnd: typeof import('@/services/websocket/streaming/handlers').handleStreamEnd
  let handleNewMessage: typeof import('@/services/websocket/streaming/handlers').handleNewMessage
  let handleTextDelta: typeof import('@/services/websocket/streaming/handlers').handleTextDelta

  const PIPELINE_ID = '39ef1314a7b9000000000000'
  const MESSAGE_ID = 'msg_a37d345d00000000'
  const THREAD_ID = 'thread-test-001'

  beforeEach(async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    pipelineStore = storeMod.usePipelineMessageStore
    pipelineStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: {},
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = handlerMod.handleStreamStart
    handleStreamEnd = handlerMod.handleStreamEnd
    handleNewMessage = handlerMod.handleNewMessage
    handleTextDelta = handlerMod.handleTextDelta
  })

  it('场景A: stream_start → stream_end 完整流程', () => {
    // 先注册 pipeline 并加载历史消息
    pipelineStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: THREAD_ID,
    })
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      { id: 'user-1', sessionId: THREAD_ID, role: 'user', content: 'hello', sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as any,
    ])

    // 1. stream_start
    handleStreamStart({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { pipeline_id: PIPELINE_ID, message_id: MESSAGE_ID, _threadId: THREAD_ID },
    })

    const afterStart = pipelineStore.getState().getMessages(PIPELINE_ID)
    console.log('After stream_start:', afterStart.map(m => ({ id: m.id?.slice(0, 16), status: m.status, seq: m.sequence })))

    const streamingMsg = afterStart.find(m => m.id === MESSAGE_ID)
    expect(streamingMsg).toBeDefined()
    expect(streamingMsg!.status).toBe('streaming')

    // 2. new_message（逐轮模型：该轮完整持久形态，content/sequence）
    handleNewMessage({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID,
        message_id: MESSAGE_ID,
        _threadId: THREAD_ID,
        sequence: 5,
        message: {
          id: MESSAGE_ID,
          role: 'assistant',
          content: 'AI response',
          sequence: 5,
          status: 'completed',
          thread_id: THREAD_ID,
        },
      },
    })

    // 3. stream_end（final_sequence 同步权威 seq）
    handleStreamEnd({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { final_sequence: 5, pipeline_id: PIPELINE_ID },
    })

    const afterEnd = pipelineStore.getState().getMessages(PIPELINE_ID)
    console.log('After stream_end:', afterEnd.map(m => ({ id: m.id?.slice(0, 16), status: m.status, seq: m.sequence })))

    const endedMsg = afterEnd.find(m => m.id === MESSAGE_ID)
    expect(endedMsg).toBeDefined()
    expect(endedMsg!.status).toBe('completed')
    expect(endedMsg!.sequence).toBe(5)
    expect((endedMsg?.content || '') + endedMsg?.parts?.map((p: any) => p.content || '').join('')).toContain('AI response')
  })

  it('场景B: stream_start 没到，text_delta 先到（自动创建占位符）', () => {
    pipelineStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: THREAD_ID,
    })

    // delta 先到，没有 stream_start
    handleTextDelta({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { pipeline_id: PIPELINE_ID, message_id: MESSAGE_ID, index: 0, text: 'partial text' },
    })

    // 等一帧让 RAF flush
    const afterChunk = pipelineStore.getState().getMessages(PIPELINE_ID)
    console.log('After chunk (no start):', afterChunk.map(m => ({ id: m.id?.slice(0, 16), status: m.status })))

    // 占位符应该被自动创建
    const placeholder = afterChunk.find(m => m.id === MESSAGE_ID)
    expect(placeholder).toBeDefined()
  })

  it('场景C: 只有 stream_end（无 stream_start 无 text_delta）', () => {
    pipelineStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: THREAD_ID,
    })
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      { id: 'user-1', sessionId: THREAD_ID, role: 'user', content: 'hello', sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as any,
    ])

    // 直接发 stream_end，没有 stream_start
    handleStreamEnd({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { full_content: 'AI response', pipeline_id: PIPELINE_ID },
    })

    const afterEnd = pipelineStore.getState().getMessages(PIPELINE_ID)
    console.log('After stream_end only:', afterEnd.map(m => ({ id: m.id?.slice(0, 16), status: m.status })))

    // stream_end 应该能正常处理（warn 但不崩溃）
    // 消息应该还在 store 中（用户消息 + 可能的 fallback）
    expect(afterEnd.length).toBeGreaterThanOrEqual(1)
  })

  it('场景D: 空轮次 stream_end（无 new_message 无内容）移除占位，后续事件不崩溃', () => {
    pipelineStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: THREAD_ID,
    })

    // 1. 初始 stream_start
    handleStreamStart({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
    })

    // 2. 中间 stream_end（前处理体/无产出轮次：无 new_message → 空轮次）
    handleStreamEnd({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { final_sequence: null, pipeline_id: PIPELINE_ID },
    })

    const afterMidEnd = pipelineStore.getState().getMessages(PIPELINE_ID)
    const midMsg = afterMidEnd.find(m => m.id === MESSAGE_ID)
    // 空轮次：消息未持久化，占位静默移除（不再补「回复内容为空」警示卡）
    expect(midMsg).toBeUndefined()

    // 3. 后续 delta（滞后到达：按占位语义自动重建）→ stream_end 不崩溃
    handleTextDelta({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      index: 0,
      text: 'final answer',
      _threadId: THREAD_ID,
    })
    handleStreamEnd({
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      data: { final_sequence: 7, pipeline_id: PIPELINE_ID },
    })

    const finalMsgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const finalMsg = finalMsgs.find(m => m.id === MESSAGE_ID)
    // 滞后 delta 正常回流：有内容 → 保留并收尾 completed（空轮次移除仅针对零内容）
    expect(finalMsg).toBeDefined()
    expect(finalMsg!.status).toBe('completed')
    expect((finalMsg?.content || '') + finalMsg?.parts?.map((p: any) => p.content || '').join('')).toContain('final answer')
  })
})
