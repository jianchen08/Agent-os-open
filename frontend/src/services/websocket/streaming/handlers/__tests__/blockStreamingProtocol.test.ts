/**
 * LLM 流式 8 事件协议前端组装测试（方案 2026-08-26 定稿）
 *
 * 断言事件 → UI 消息结构的映射（blockHandler.ts）：
 * - block_start/text_delta/reasoning_delta/tool_call_delta/block_end 按块索引
 *   组装出 thinking/text/tool_call parts（思考区 / 正文增量 / 工具卡片参数）
 * - tool_call_delta 的 arguments_delta 原始 JSON 片段累积，block_end 时解析为
 *   args（渲染卡片参数区，不渲染原始 JSON 增量）
 * - usage 落入 usage store；finish 清理块状态（后续事件不再生效）
 * - keepalive 无前端消费面（不订阅不处理，心跳语义由连接层保证）
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

const PIPELINE_ID = 'pipe-block-protocol-001'
const MESSAGE_ID = 'msg_block_protocol_01'
const THREAD_ID = 'thread-block-protocol-001'

/** 构造后端 WS 事件信封（内核透传补路由键：业务字段在 data 下） */
function makeEvent(eventType: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: { pipeline_id: PIPELINE_ID, message_id: MESSAGE_ID, ...data },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

describe('LLM 流式 8 事件协议组装', () => {
  let usePipelineMessageStore: any
  let useContextUsageStore: any
  let h: Record<string, any>

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
    const usageMod = await import('@/stores/contextUsageStore')
    useContextUsageStore = usageMod.useContextUsageStore

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    h = handlerMod
    h.handleStreamStart(makeEvent('stream_start', {}))
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  function snapshotParts() {
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
    return (msg?.parts || []).map((p: any) => ({
      type: p.type,
      content: p.content || '',
      state: p.state,
      callId: p.callId,
      name: p.name,
      args: p.args,
      result: p.result,
    }))
  }

  it('完整序列：思考块 → 正文块 → 工具块，thinking/text 由块组装、tool_call 不建卡（契约事件建卡）', async () => {
    // 思考块（块索引 0）
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    h.handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '让我想想' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning' } }))

    // 正文块（块索引 1）：多段增量累积
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 1, text: '第一段' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 1, text: '第二段' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'text' } }))

    // 工具块（块索引 2）：块协议不建卡——工具卡面由契约事件 tool_start/tool_result
    // 创建（三源建卡会导致同一工具两张卡，见 blockHandler 注释）
    h.handleBlockStart(makeEvent('block_start', { index: 2, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, id: 'call-1' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, name: 'search' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, arguments_delta: '{"q":' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, arguments_delta: '"天气"}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 2, block: { block_type: 'tool_call' } }))
    // 契约事件建立工具卡（单卡）
    h.handleToolStart(makeEvent('tool_start', { call_id: 'call-1', tool_name: 'search', args: { q: '天气' } }))

    const parts = snapshotParts()
    console.log('[完整序列] parts:', JSON.stringify(parts, null, 2))

    // 类型序列：块组装 thinking/text + 契约事件建 tool_call（块打开顺序保持）
    expect(parts.map((p: any) => p.type)).toEqual(['thinking', 'text', 'tool_call'])

    // 思考区：内容完整、done
    expect(parts[0].content).toBe('让我想想')
    expect(parts[0].state).toBe('done')

    // 正文增量累积
    expect(parts[1].content).toBe('第一段第二段')
    expect(parts[1].state).toBe('done')

    // 工具卡唯一：callId 来自契约事件
    expect(parts.filter((p: any) => p.type === 'tool_call').length).toBe(1)
    expect(parts[2].callId).toBe('call-1')
    expect(parts[2].name).toBe('search')
    expect(parts[2].args).toEqual({ q: '天气' })
  })

  it('tool_call_delta 不建卡（无论有无 id）：工具卡面唯一来源 = tool_start/tool_result', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, arguments_delta: '{"a":1}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'tool_call' } }))

    const parts = snapshotParts()
    expect(parts.find((p: any) => p.type === 'tool_call')).toBeUndefined()
    // 契约事件到达后出现唯一一张卡
    h.handleToolStart(makeEvent('tool_start', { call_id: 'call-a1', tool_name: 'f', args: { a: 1 } }))
    const toolParts = snapshotParts().filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('call-a1')
    expect(toolParts[0].args).toEqual({ a: 1 })
  })

  it('工具 arguments 非法 JSON：块侧不消费（不崩）；卡面 args 以契约事件为准', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'tc-x', name: 'f', arguments_delta: '{oops' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'tool_call' } }))
    // 契约事件带完整 args
    h.handleToolStart(makeEvent('tool_start', { call_id: 'tc-x', tool_name: 'f', args: { a: 1 } }))

    const parts = snapshotParts()
    const tool = parts.find((p: any) => p.type === 'tool_call')
    expect(tool).toBeDefined()
    expect(tool.args).toEqual({ a: 1 })
    expect(tool.state).toBe('calling')
  })

  it('usage 事件落入 usage store（input/output tokens）', () => {
    h.handleUsage(makeEvent('usage', {
      input_tokens: 120, output_tokens: 30, total_tokens: 150, cached_tokens: 10,
    }))
    const usage = useContextUsageStore.getState().getUsage(PIPELINE_ID)
    expect(usage).toBeDefined()
    // usage store 归一化字段：promptTokens/completionTokens
    expect(usage?.promptTokens).toBe(120)
    expect(usage?.completionTokens).toBe(30)
    expect(usage?.totalTokens).toBe(150)
  })

  it('finish 清理块状态：其后到达的增量不再写入（流已终结）', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 0, text: '正文' }))
    h.handleFinish(makeEvent('finish', { reason: 'stop' }))
    await vi.advanceTimersByTimeAsync(16)

    // finish flush 后内容落盘
    let parts = snapshotParts()
    expect(parts.find((p: any) => p.type === 'text')?.content).toBe('正文')

    // finish 后同一块索引再发增量：块状态已清，新块重新登记（防御性不串写）
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 0, text: '新内容' }))
    await vi.advanceTimersByTimeAsync(16)

    parts = snapshotParts()
    const textParts = parts.filter((p: any) => p.type === 'text')
    // 新块独立 part（块状态已清，重新登记块 0）
    expect(textParts.length).toBe(2)
    expect(textParts[1].content).toBe('新内容')
  })

  it('工具块未闭合（block_end 丢失）→ 不建卡；卡面由 stream_end 权威快照合并（成功后 state=done）', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'tc-u', name: 'f', arguments_delta: '{}' }))
    // 不发 block_end，直接 stream_end（内核收尾裁决）——块侧从未建卡
    expect(snapshotParts().find((p: any) => p.type === 'tool_call')).toBeUndefined()

    h.handleStreamEnd(makeEvent('stream_end', {
      full_content: '', final_sequence: 3,
      parts: [{ type: 'tool_call', callId: 'tc-u', name: 'f', args: {}, state: 'done', sequence: 2 }],
    }))

    const parts = snapshotParts()
    const tool = parts.find((p: any) => p.type === 'tool_call')
    expect(tool).toBeDefined()
    expect(tool.state).toBe('done')
  })
})
