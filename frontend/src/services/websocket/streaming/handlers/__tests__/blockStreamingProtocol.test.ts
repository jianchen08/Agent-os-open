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
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-block-protocol-001'
const MESSAGE_ID = 'msg_block_protocol_01'
const THREAD_ID = 'thread-block-protocol-001'

/** 构造后端 WS 事件信封（内核透传补路由键：业务字段在 data 下） */
const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

describe('LLM 流式 8 事件协议组装', () => {
  let usePipelineMessageStore: any
  let useContextUsageStore: any
  let h: Record<string, any>

  beforeEach(async () => {
    vi.useFakeTimers()
    usePipelineMessageStore = await resetStreamingStore(PIPELINE_ID, THREAD_ID)
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
      durationMs: p.durationMs,
    }))
  }

  it('完整序列：思考 → 正文 → 工具块输出即建卡，block_end 填参数，契约事件归并单卡贯穿到终态', async () => {
    // 思考块（块索引 0）
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    h.handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '让我想想' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning' } }))

    // 正文块（块索引 1）：多段增量累积
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'text' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 1, text: '第一段' }))
    h.handleTextDelta(makeEvent('text_delta', { index: 1, text: '第二段' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'text' } }))

    // 工具块（块索引 2）：首个携带 id+name 的增量即建卡（OpenAI 兼容流首增量
    // 即带 id+name+arguments_delta，llm_service streaming.py 契约）——输出阶段
    // running 可见，不等契约事件
    h.handleBlockStart(makeEvent('block_start', { index: 2, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, id: 'call-1', name: 'search', arguments_delta: '{"q":' }))
    let toolPart = snapshotParts().find((p: any) => p.type === 'tool_call')
    expect(toolPart).toMatchObject({ callId: 'call-1', name: 'search', state: 'calling', args: {} })
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 2, arguments_delta: '"天气"}' }))
    // block_end 携带完整累积载荷（id/name/arguments 原始 JSON 串）→ 解析填入 args
    h.handleBlockEnd(makeEvent('block_end', { index: 2, block: { block_type: 'tool_call', id: 'call-1', name: 'search', arguments: '{"q":"天气"}' } }))
    toolPart = snapshotParts().find((p: any) => p.type === 'tool_call')
    expect(toolPart.args).toEqual({ q: '天气' })

    // 契约事件 tool_start/tool_result 按 call_id 归并进同一张卡（不建第二张）
    h.handleToolStart(makeEvent('tool_start', { call_id: 'call-1', tool_name: 'search', args: { q: '天气' } }))
    expect(snapshotParts().filter((p: any) => p.type === 'tool_call').length).toBe(1)
    h.handleToolResult(makeEvent('tool_result', { call_id: 'call-1', tool_name: 'search', result: '晴 26℃', success: true, duration_ms: 12.5 }))

    const parts = snapshotParts()

    // 类型序列：块打开顺序保持（thinking → text → tool_call）
    expect(parts.map((p: any) => p.type)).toEqual(['thinking', 'text', 'tool_call'])
    expect(parts[0].content).toBe('让我想想')
    expect(parts[0].state).toBe('done')
    expect(parts[1].content).toBe('第一段第二段')
    expect(parts[1].state).toBe('done')

    // 单卡贯穿：callId/name/args 源自输出阶段，result/duration 源自契约事件
    const toolParts = parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0]).toMatchObject({
      callId: 'call-1', name: 'search', args: { q: '天气' },
      state: 'done', result: '晴 26℃', durationMs: 12.5,
    })
  })

  it('增量缺 id/name → 不建卡（降级，卡由契约事件建）；对照：携带 id+name 的增量即建卡且契约事件归并不重复', async () => {
    // 向量①：provider 不发 id/name（增量仅 arguments_delta，block_end 载荷也无 id）
    // → 块侧不建卡（无法与契约事件的 call_id 归并），卡由 tool_start 建
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, arguments_delta: '{"a":1}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'tool_call', arguments: '{"a":1}' } }))
    expect(snapshotParts().find((p: any) => p.type === 'tool_call')).toBeUndefined()

    h.handleToolStart(makeEvent('tool_start', { call_id: 'call-a1', tool_name: 'f', args: { a: 1 } }))
    const vector1 = snapshotParts().filter((p: any) => p.type === 'tool_call')
    expect(vector1.length).toBe(1)
    expect(vector1[0]).toMatchObject({ callId: 'call-a1', args: { a: 1 } })

    // 向量②：同序列对照——增量携带 id+name → delta 即建卡，tool_start 按 call_id
    // 归并进这张卡（单卡不变式）
    h.handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 1, id: 'call-b2', name: 'f', arguments_delta: '{"a":1}' }))
    h.handleBlockEnd(makeEvent('block_end', { index: 1, block: { block_type: 'tool_call', id: 'call-b2', name: 'f', arguments: '{"a":1}' } }))
    h.handleToolStart(makeEvent('tool_start', { call_id: 'call-b2', tool_name: 'f', args: { a: 1 } }))

    const vector2 = snapshotParts().filter((p: any) => p.type === 'tool_call' && p.callId === 'call-b2')
    expect(vector2.length).toBe(1)
    expect(vector2[0].args).toEqual({ a: 1 })
  })

  it('工具 arguments 非法 JSON：block_end 解析失败不覆盖既有 args（不崩）；卡面 args 以契约事件为准', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'tc-x', name: 'f', arguments_delta: '{oops' }))
    // block_end 载荷 arguments 非法 → 解析失败，args 保持建卡时的 {}
    h.handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'tool_call', id: 'tc-x', name: 'f', arguments: '{oops' } }))
    let tool = snapshotParts().find((p: any) => p.type === 'tool_call')
    expect(tool).toBeDefined()
    expect(tool.args).toEqual({})
    // 契约事件携带完整 args（最终权威）
    h.handleToolStart(makeEvent('tool_start', { call_id: 'tc-x', tool_name: 'f', args: { a: 1 } }))

    tool = snapshotParts().find((p: any) => p.type === 'tool_call')
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

  it('block_end 丢失 → 卡保持 calling（stream_end 快照不伪造工具终态）；tool_start/tool_result 归并收尾（生产时序）', async () => {
    h.handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'tool_call' }))
    h.handleToolCallDelta(makeEvent('tool_call_delta', { index: 0, id: 'tc-u', name: 'f', arguments_delta: '{}' }))
    // 不发 block_end：卡已在（输出阶段建），保持 calling
    expect(snapshotParts().find((p: any) => p.type === 'tool_call')).toMatchObject({ callId: 'tc-u', state: 'calling' })

    // 轮收尾 stream_end（内核在工具执行前的轮边界发）——快照合并不把工具卡
    // 伪造为终态：终态只能来自 tool_result/new_message 的结果证据
    h.handleStreamEnd(makeEvent('stream_end', {
      full_content: '', final_sequence: 3,
      parts: [{ type: 'tool_call', callId: 'tc-u', name: 'f', args: {}, state: 'done', sequence: 2 }],
    }))
    expect(snapshotParts().filter((p: any) => p.type === 'tool_call').length).toBe(1)

    // 工具迭代（下一轮）契约事件归并收尾
    h.handleToolStart(makeEvent('tool_start', { call_id: 'tc-u', tool_name: 'f', args: {} }))
    expect(snapshotParts().find((p: any) => p.type === 'tool_call')?.state).toBe('calling')
    h.handleToolResult(makeEvent('tool_result', { call_id: 'tc-u', tool_name: 'f', result: 'ok', success: true, duration_ms: 3 }))

    const tool = snapshotParts().find((p: any) => p.type === 'tool_call')
    expect(tool).toMatchObject({ callId: 'tc-u', state: 'done', result: 'ok' })
  })
})
