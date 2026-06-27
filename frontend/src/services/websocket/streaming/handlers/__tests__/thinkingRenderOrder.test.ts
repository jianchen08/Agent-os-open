/**
 * 思考过程渲染顺序复现测试
 *
 * 复现用户报告："思考过程渲染顺序出问题了"，刷新后依旧。
 *
 * 渲染层 buildFragmentsFromParts 按 part.sequence 升序渲染。
 * 本测试驱动真实 WS handler（thinking_start/chunk/end + stream_chunk + stream_end），
 * 断言最终 parts 的 sequence 数值顺序与逻辑顺序（思考在前、文本在后）一致。
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

const PIPELINE_ID = 'pipe-thinking-order-001'
const MESSAGE_ID = 'msg_thinking_order_01'
const THREAD_ID = 'thread-thinking-order-001'

/**
 * 构造后端 WS 事件信封（与 bridge_core._make_event 一致：业务字段在 data 下）。
 * sequence 是 part 级序号，后端按发送顺序递增（thinking_start 先于 stream_chunk）。
 */
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

describe('思考过程渲染顺序：流式 thinking + text 的 part.sequence', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let handleStreamStart: typeof import('@/services/websocket/streaming/handlers').handleStreamStart
  let handleStreamChunk: typeof import('@/services/websocket/streaming/handlers').handleStreamChunk
  let handleStreamEnd: typeof import('@/services/websocket/streaming/handlers').handleStreamEnd
  let handleThinkingStart: typeof import('@/services/websocket/streaming/handlers').handleThinkingStart
  let handleThinkingChunk: typeof import('@/services/websocket/streaming/handlers').handleThinkingChunk
  let handleThinkingEnd: typeof import('@/services/websocket/streaming/handlers').handleThinkingEnd

  beforeEach(async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
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

  /**
   * 取最终消息的 parts，按 sequence 升序得到渲染顺序，断言思考在文本之前。
   */
  function getRenderOrder() {
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m) => m.id === MESSAGE_ID)
    if (!msg || !msg.parts) return [] as { type: string; sequence: number; content: string }[]
    return [...msg.parts]
      .sort((a: any, b: any) => (a.sequence ?? 0) - (b.sequence ?? 0))
      .map((p: any) => ({ type: p.type, sequence: p.sequence, content: p.content }))
  }

  it('标准流程：thinking_start(seq=1) → thinking_chunk → thinking_end → stream_chunk(seq=2)，思考在文本之前', () => {
    // 1. stream_start 创建占位消息
    handleStreamStart(makeEvent('stream_start', {}))

    // 2. 思考开始（后端分配 sequence=1）
    handleThinkingStart(makeEvent('thinking_start', { sequence: 1 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '我在思考...', sequence: 2 }))
    handleThinkingEnd(makeEvent('thinking_end', { duration_ms: 100 }))

    // 3. 正文文本（后端分配 sequence=3）
    handleStreamChunk(makeEvent('stream_chunk', { content: '最终回复', sequence: 3 }))
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '最终回复', final_sequence: 5,
      parts: [
        { type: 'thinking', content: '我在思考...', state: 'done', sequence: 1 },
        { type: 'text', content: '最终回复', state: 'done', sequence: 3 },
      ],
    }))

    const order = getRenderOrder()
    console.log('parts render order:', JSON.stringify(order, null, 2))

    // 断言：思考 part 的 sequence < 文本 part 的 sequence（思考渲染在前）
    const thinkIdx = order.findIndex((p) => p.type === 'thinking')
    const textIdx = order.findIndex((p) => p.type === 'text')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(textIdx).toBeGreaterThanOrEqual(0)
    expect(thinkIdx).toBeLessThan(textIdx)
  })

  it('后端 thinking 事件未携带 sequence（部分模型/适配器场景）→ 不应把思考排到文本之后', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // thinking_start / chunk 都不带 sequence 字段
    handleThinkingStart(makeEvent('thinking_start', {}))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '无序号思考...' }))
    handleThinkingEnd(makeEvent('thinking_end', { duration_ms: 100 }))

    // 文本带 sequence=3
    handleStreamChunk(makeEvent('stream_chunk', { content: '最终回复', sequence: 3 }))
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '最终回复', final_sequence: 5,
    }))

    const order = getRenderOrder()
    console.log('parts render order (no seq on thinking):', JSON.stringify(order, null, 2))

    const thinkIdx = order.findIndex((p) => p.type === 'thinking')
    const textIdx = order.findIndex((p) => p.type === 'text')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(textIdx).toBeGreaterThanOrEqual(0)
    // 关键断言：思考必须在文本之前
    expect(thinkIdx).toBeLessThan(textIdx)
  })
})
