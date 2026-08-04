/**
 * 思考过程渲染顺序回归测试
 *
 * 渲染顺序由两部分共同保证：
 *  - 后端时序：thinking_start → thinking_chunk → thinking_end → stream_chunk（同轮内思考先于正文）
 *  - parts 保序渲染：buildFragmentsFromParts 严格按 parts 数组顺序渲染，不再做"thinking 前置"重排
 *
 * 多轮 LLM 调用应得到交错顺序（思考1→正文1→思考2→正文2），每轮思考各一个独立卡片，
 * 且流式态与最终态一致。
 *
 * 本测试驱动真实 WS handler（thinking_start/chunk/end + stream_chunk + stream_end + tool），
 * 断言最终 parts 的顺序与逻辑顺序一致。
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

describe('多轮 LLM 调用：思考卡片按交错顺序、流式态与最终态一致', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let handleStreamStart: typeof import('@/services/websocket/streaming/handlers').handleStreamStart
  let handleStreamChunk: typeof import('@/services/websocket/streaming/handlers').handleStreamChunk
  let handleStreamEnd: typeof import('@/services/websocket/streaming/handlers').handleStreamEnd
  let handleThinkingStart: typeof import('@/services/websocket/streaming/handlers').handleThinkingStart
  let handleThinkingChunk: typeof import('@/services/websocket/streaming/handlers').handleThinkingChunk
  let handleThinkingEnd: typeof import('@/services/websocket/streaming/handlers').handleThinkingEnd
  let handleToolStart: typeof import('@/services/websocket/streaming/handlers').handleToolStart
  let handleToolResult: typeof import('@/services/websocket/streaming/handlers').handleToolResult

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
    handleToolStart = handlerMod.handleToolStart
    handleToolResult = handlerMod.handleToolResult
  })

  /** 取最终消息 parts 的类型序列（按数组顺序=渲染顺序） */
  function getPartTypes(): string[] {
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m) => m.id === MESSAGE_ID)
    if (!msg || !msg.parts) return []
    return msg.parts.map((p: any) => p.type)
  }

  /** 取所有 thinking part 的 state 列表 */
  function getThinkingStates(): string[] {
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m) => m.id === MESSAGE_ID)
    if (!msg || !msg.parts) return []
    return msg.parts.filter((p: any) => p.type === 'thinking').map((p: any) => p.state)
  }

  it('两轮 LLM 调用（含工具）→ parts 类型序列为交错 [thinking, tool_call, thinking, text]', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // 第一轮：思考 → 工具
    handleThinkingStart(makeEvent('thinking_start', { sequence: 1 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '第一轮思考', sequence: 1 }))
    handleThinkingEnd(makeEvent('thinking_end', { duration_ms: 100 }))
    handleToolStart(makeEvent('tool_start', { call_id: 'tc-1', tool_name: 'search', args: {}, sequence: 2 }))
    handleToolResult(makeEvent('tool_result', { call_id: 'tc-1', tool_name: 'search', result: '结果1', success: true }))

    // 第二轮：思考 → 正文
    handleThinkingStart(makeEvent('thinking_start', { sequence: 3 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '第二轮思考', sequence: 3 }))
    handleThinkingEnd(makeEvent('thinking_end', { duration_ms: 80 }))
    handleStreamChunk(makeEvent('stream_chunk', { content: '最终回复', sequence: 4 }))
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '最终回复', final_sequence: 5,
    }))

    const types = getPartTypes()
    console.log('多轮 parts 类型序列:', JSON.stringify(types))

    // 关键断言：交错顺序，两个 thinking 各自留在对应位置，没有被堆到最前
    expect(types).toEqual(['thinking', 'tool_call', 'thinking', 'text'])
  })

  it('第一轮 thinking_end 丢失：第二轮 thinking_start 应先 finalize 上一轮，再开新 part（两轮各一卡片）', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // 第一轮思考开始 + chunk，但【故意不发 thinking_end】
    handleThinkingStart(makeEvent('thinking_start', { sequence: 1 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '第一轮思考', sequence: 1 }))

    // 第一轮残留 streaming 的 thinking part
    expect(getThinkingStates()).toEqual(['streaming'])

    // 第二轮 thinking_start 到达：应把第一轮兜底置 done，再创建新的 streaming part
    handleThinkingStart(makeEvent('thinking_start', { sequence: 2 }))
    handleThinkingChunk(makeEvent('thinking_chunk', { content: '第二轮思考', sequence: 2 }))
    handleThinkingEnd(makeEvent('thinking_end', { duration_ms: 50 }))

    const states = getThinkingStates()
    console.log('两轮 thinking part 状态（第一轮 end 丢失）:', JSON.stringify(states))

    // 关键断言：得到两个 thinking part，第一个被兜底置 done，第二个正常
    expect(states).toEqual(['done', 'done'])
    expect(getPartTypes()).toEqual(['thinking', 'thinking'])
  })
})
