/**
 * 工具调用与文本块顺序回归测试（LLM 流式 8 事件协议，方案 2026-08-26）
 *
 * 复现用户报告："流式消息渲染到思考内容上面，出现思考-工具-文本-文本-思考"。
 *
 * 根因假设（旧 stream_chunk 时代的竞态，协议迁移后结构复述）：
 *  text_delta 只把正文增量缓冲到 RAF（不立即创建 text part），而
 *  handleToolStart 同步执行。当 tool_start 在 RAF flush 之前到达：
 *    1. text part 尚未创建（delta 仍在 buffer）
 *    2. tool_start 的 findStreamingPartIndex → -1，不关闭任何 part，append tool_call
 *    3. 随后 RAF flush，findStreamingPartIndex 仍 → -1，新建 text part 追加到末尾（tool_call 之后）
 *    4. 结果 [tool_call, text]，正文被劈到工具后面
 *
 * 本测试用 fake timers 精确控制 RAF 时序，证实该假设在块协议下仍被防住
 * （text_delta 缓冲按块索引路由，tool_start 的 flush 语义不变）。
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

const PIPELINE_ID = 'pipe-tool-text-order-001'
const MESSAGE_ID = 'msg_tool_text_order_01'
const THREAD_ID = 'thread-tool-text-order-001'

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

/** 取 parts 的类型序列（按数组顺序 = 渲染顺序） */
function getPartTypes(): string[] {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg || !msg.parts) return []
  return msg.parts.map((p: any) => p.type)
}

/** 取 text part 的内容序列（按数组顺序） */
function getTextContents(): string[] {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg || !msg.parts) return []
  return msg.parts.filter((p: any) => p.type === 'text').map((p: any) => p.content || '')
}

describe('工具调用与文本块顺序：tool_start 与 buffered text delta 的时序竞争', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
  let handleTextDelta: any
  let handleStreamEnd: any
  let handleToolStart: any
  let handleToolResult: any
  let handleBlockStart: any
  let handleReasoningDelta: any
  let handleBlockEnd: any

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
    handleTextDelta = handlerMod.handleTextDelta
    handleStreamEnd = handlerMod.handleStreamEnd
    handleToolStart = handlerMod.handleToolStart
    handleToolResult = handlerMod.handleToolResult
    handleBlockStart = handlerMod.handleBlockStart
    handleReasoningDelta = handlerMod.handleReasoningDelta
    handleBlockEnd = handlerMod.handleBlockEnd
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('复现：text delta buffered 未 flush 时 tool_start 到达 → 文本应保持在工具之前', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 正文文本 delta（缓冲到 RAF，不立即创建 text part）
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 0, text: '工具调用前的正文', sequence: 2 }))
    // 【关键】不推进 RAF —— 镜像"tool_start 在同一帧内紧随 delta 到达"

    // tool_start 同步到达：此时 text part 已由 block_start 预创建（streaming）
    handleToolStart(makeEvent('tool_start', {
      call_id: 'tc-1', tool_name: 'search', args: {}, sequence: 3,
    }))

    // 现在推进 RAF，buffered delta 才被 flush
    await vi.advanceTimersByTimeAsync(16)

    const types = getPartTypes()
    const texts = getTextContents()
    console.log('[复现] parts 类型序列:', JSON.stringify(types))
    console.log('[复现] text 内容:', JSON.stringify(texts))

    // ✅ 期望顺序：[text, tool_call]（正文在前，工具在后）
    expect(types).toEqual(['text', 'tool_call'])
    expect(texts).toEqual(['工具调用前的正文'])
  })

  it('完整多轮：思考1→正文1→工具→思考2→正文2（不应出现思考-工具-文本-文本-思考）', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 第一轮：思考 → 正文1
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '第一轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning' } }))
    handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 1, text: '正文1' }))
    // 不推进 RAF（镜像真实主线程时序）

    // 工具调用（同步）
    handleToolStart(makeEvent('tool_start', {
      call_id: 'tc-1', tool_name: 'search', args: {}, sequence: 3,
    }))
    handleToolResult(makeEvent('tool_result', {
      call_id: 'tc-1', tool_name: 'search', result: '结果', success: true,
    }))

    // 第二轮：思考2 → 正文2
    handleBlockStart(makeEvent('block_start', { index: 2, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 2, text: '第二轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 2, block: { block_type: 'reasoning' } }))
    handleBlockStart(makeEvent('block_start', { index: 3, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 3, text: '正文2' }))

    handleStreamEnd(makeEvent('stream_end', {
      full_content: '正文1\n\n正文2', final_sequence: 6,
    }))

    const types = getPartTypes()
    const texts = getTextContents()
    console.log('[多轮] parts 类型序列:', JSON.stringify(types))
    console.log('[多轮] text 内容:', JSON.stringify(texts))

    // ✅ 期望交错顺序：思考1→正文1→工具→思考2→正文2
    expect(types).toEqual(['thinking', 'text', 'tool_call', 'thinking', 'text'])
    // 文本不被劈成多余段
    expect(texts).toEqual(['正文1', '正文2'])
  })
})
