/**
 * 思考过程渲染顺序回归测试（LLM 流式 8 事件协议，方案 2026-08-26 定稿）
 *
 * 渲染顺序由两部分共同保证：
 *  - 后端时序：block_start(reasoning) → reasoning_delta → block_end →
 *    block_start(text) → text_delta（同轮内思考先于正文，块索引递增）
 *  - parts 保序渲染：buildFragmentsFromParts 严格按 parts 数组顺序渲染，
 *    不做"thinking 前置"重排
 *
 * 多轮 LLM 调用应得到交错顺序（思考1→正文1→思考2→正文2），每轮思考各一个
 * 独立卡片，且流式态与最终态一致。
 *
 * 本测试驱动真实 WS handler（block_start/reasoning_delta/text_delta/block_end +
 * stream_end），断言最终 parts 的顺序与逻辑顺序一致。
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
 * 构造后端 WS 事件信封（与内核透传一致：业务字段在 data 下）。
 * block 协议事件携带 index（块索引，text/reasoning/tool-call 共享递增序列）。
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

describe('思考过程渲染顺序：流式 reasoning + text 的 part 顺序', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let handleStreamStart: typeof import('@/services/websocket/streaming/handlers').handleStreamStart
  let handleTextDelta: typeof import('@/services/websocket/streaming/handlers').handleTextDelta
  let handleStreamEnd: typeof import('@/services/websocket/streaming/handlers').handleStreamEnd
  let handleBlockStart: typeof import('@/services/websocket/streaming/handlers').handleBlockStart
  let handleBlockEnd: typeof import('@/services/websocket/streaming/handlers').handleBlockEnd
  let handleReasoningDelta: typeof import('@/services/websocket/streaming/handlers').handleReasoningDelta
  let handleToolCallDelta: typeof import('@/services/websocket/streaming/handlers').handleToolCallDelta

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
    handleTextDelta = handlerMod.handleTextDelta
    handleStreamEnd = handlerMod.handleStreamEnd
    handleBlockStart = handlerMod.handleBlockStart
    handleBlockEnd = handlerMod.handleBlockEnd
    handleReasoningDelta = handlerMod.handleReasoningDelta
    handleToolCallDelta = handlerMod.handleToolCallDelta
  })

  /** 取最终消息的 parts 类型序列（按数组顺序=渲染顺序） */
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

  it('标准流程：reasoning 块(0) → text 块(1)，思考在文本之前', () => {
    // 1. stream_start 创建占位消息
    handleStreamStart(makeEvent('stream_start', {}))

    // 2. 思考块（块索引 0）
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '我在思考...' }))
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning', text: '我在思考...' } }))

    // 3. 正文块（块索引 1）
    handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 1, text: '最终回复' }))
    handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'text', text: '最终回复' } }))
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '最终回复', final_sequence: 5,
      parts: [
        { type: 'thinking', content: '我在思考...', state: 'done', sequence: 1 },
        { type: 'text', content: '最终回复', state: 'done', sequence: 3 },
      ],
    }))

    const types = getPartTypes()
    console.log('parts type sequence:', JSON.stringify(types))

    // 断言：思考 part 在文本 part 之前（渲染顺序）
    const thinkIdx = types.indexOf('thinking')
    const textIdx = types.indexOf('text')
    expect(thinkIdx).toBeGreaterThanOrEqual(0)
    expect(textIdx).toBeGreaterThanOrEqual(0)
    expect(thinkIdx).toBeLessThan(textIdx)
  })

  it('两轮 LLM 调用（含工具）→ parts 类型序列为交错 [thinking, tool_call, thinking, text]', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // 第一轮：思考块(0) → 工具块(1)
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '第一轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning', text: '第一轮思考' } }))
    handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'tool_call' }))
    handleToolCallDelta(makeEvent('tool_call_delta', { index: 1, id: 'tc-1', name: 'search', arguments_delta: '{}' }))
    handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'tool_call', id: 'tc-1', name: 'search', arguments: '{}' } }))

    // 第二轮：思考块(2) → 正文块(3)
    handleBlockStart(makeEvent('block_start', { index: 2, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 2, text: '第二轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 2, block: { block_type: 'reasoning', text: '第二轮思考' } }))
    handleBlockStart(makeEvent('block_start', { index: 3, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 3, text: '最终回复' }))
    handleBlockEnd(makeEvent('block_end', { index: 3, block: { block_type: 'text', text: '最终回复' } }))
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '最终回复', final_sequence: 5,
    }))

    const types = getPartTypes()
    console.log('多轮 parts 类型序列:', JSON.stringify(types))

    // 关键断言：交错顺序，两个 thinking 各自留在对应位置，没有被堆到最前
    expect(types).toEqual(['thinking', 'tool_call', 'thinking', 'text'])
  })

  it('第一轮 reasoning 块未闭合：第二轮 reasoning 块应新建独立卡片（两轮各一）', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // 第一轮思考块开始 + delta，但【故意不发 block_end】
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '第一轮思考' }))
    expect(getThinkingStates()).toEqual(['streaming'])

    // 第二轮思考块（新块索引 1，独立卡片）
    handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 1, text: '第二轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'reasoning' } }))
    // 收尾：第一轮残留 streaming 的 thinking part 由 stream_end 合并兜底收敛
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '', final_sequence: 5,
      parts: [
        { type: 'thinking', content: '第一轮思考', state: 'done', sequence: 1 },
        { type: 'thinking', content: '第二轮思考', state: 'done', sequence: 2 },
      ],
    }))

    const states = getThinkingStates()
    console.log('两轮 thinking part 状态（第一轮 block_end 丢失）:', JSON.stringify(states))

    // 关键断言：两个 thinking part（各自独立卡片），stream_end 兜底全部 done
    expect(states).toEqual(['done', 'done'])
    expect(getPartTypes()).toEqual(['thinking', 'thinking'])
  })
})
