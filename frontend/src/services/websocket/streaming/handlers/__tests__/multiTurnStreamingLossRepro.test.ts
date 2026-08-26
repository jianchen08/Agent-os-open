/**
 * 流式多轮工具调用消息丢失复现测试（LLM 流式 8 事件协议，方案 2026-08-26）
 *
 * 对应用户反馈 bug：多轮工具调用流式对话中，第一次流式输出时有消息，
 * 之后的工具调用轮次消息丢失（工具卡片不显示 / 后续 assistant 内容丢失）。
 *
 * 背景：
 * - 之前修复了静态加载路径（mergeConsecutiveAssistantMessages 的 toolMessages 保留），
 *   用户实测问题仍在 → 丢失发生在流式输出路径（WS handler 增量追加消息时）。
 *
 * 被测链路（真实 handler + 真实 pipelineMessageStore + 真实 RAF 批处理）：
 *   stream_start → reasoning/text delta → tool delta → tool_result
 *   → 第二轮 text delta → new_message（后端收尾，携带权威 parts）
 *
 * 正文增量由 text_delta{index, text} 表达（旧 stream_chunk 退役）；
 * 工具调用仍由 tool_start/tool_result 事件承载（tool_core 引擎路径未迁移）。
 *
 * 核心回归断言（对应"流式多轮工具调用完整显示"）：
 *   1. 多轮工具调用的所有轮次内容（thinking/text/tool_call）必须完整保留
 *   2. new_message 收尾不得覆盖本地已累积的 tool_call parts（serverParts 只有 text）
 *   3. RAF buffer 未 flush 时 new_message 到达，本地内容不丢失
 *   4. tool_start 到达时消息占位不存在（乱序/占位丢失），工具调用不静默丢弃
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
  // messageHandler 的 data.message 完整形态路径依赖共享 mapper（本文件不测该路径，
  // 提供最小实现保持模块契约有效；完整行为见 newMessageFullShape.test.ts）
  mapBackendMessageToMessage: (m: any, sessionId: string) => ({
    id: m.id,
    sessionId,
    sequence: m.sequence ?? 0,
    role: m.role,
    content: m.content || '',
    timestamp: m.timestamp || '',
    status: m.status || 'completed',
    parts: [],
  }),
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pipe-stream-multiturn-loss-001'
const MESSAGE_ID = 'msg_stream_multiturn_loss_01'
const THREAD_ID = 'thread-stream-multiturn-loss-001'

/** 构造后端 WS 事件信封（与 ws_session.rs / capability_router.rs 一致：业务字段在 data 下） */
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

/** 取目标消息的完整快照 */
function snapshotMessage() {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg) return { found: false, content: '', parts: [], status: '', role: '' }
  return {
    found: true,
    content: msg.content || '',
    status: msg.status,
    role: msg.role,
    parts: (msg.parts || []).map((p: any) => ({
      type: p.type,
      content: p.content || '',
      state: p.state,
      callId: p.callId,
      name: p.name,
      result: p.result,
    })),
  }
}

describe('流式多轮工具调用消息丢失复现', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
  let handleTextDelta: any
  let handleBlockStart: any
  let handleBlockEnd: any
  let handleReasoningDelta: any
  let handleNewMessage: any
  let handleToolStart: any
  let handleToolResult: any

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
    handleBlockStart = handlerMod.handleBlockStart
    handleBlockEnd = handlerMod.handleBlockEnd
    handleReasoningDelta = handlerMod.handleReasoningDelta
    handleNewMessage = handlerMod.handleNewMessage
    handleToolStart = handlerMod.handleToolStart
    handleToolResult = handlerMod.handleToolResult
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('场景1（完整时序）：stream_start→思考→正文→工具→第二轮正文→new_message，所有轮次内容完整保留', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 第一轮：思考 + 正文
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '第一轮思考' }))
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning', text: '第一轮思考' } }))
    handleBlockStart(makeEvent('block_start', { index: 1, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 1, text: '第一轮正文' }))
    await vi.advanceTimersByTimeAsync(16)

    // 工具调用（tool_core 事件路径未迁移）
    handleToolStart(makeEvent('tool_start', {
      call_id: 'tc-1', tool_name: 'search', args: { q: 'test' }, sequence: 3,
    }))
    handleToolResult(makeEvent('tool_result', {
      call_id: 'tc-1', tool_name: 'search', result: '搜索结果1', success: true,
    }))

    // 第二轮正文（工具调用后，新块索引）
    handleBlockStart(makeEvent('block_start', { index: 2, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 2, text: '基于工具结果的回答' }))
    await vi.advanceTimersByTimeAsync(16)

    // 后端 new_message 收尾（Rust 内核 ws_session.rs 的 serverParts 只有 text）
    handleNewMessage(makeEvent('new_message', {
      content: '基于工具结果的回答',
      parts: [{ type: 'text', text: '基于工具结果的回答' }],
      sequence: 5,
    }))

    const snap = snapshotMessage()
    console.log('[场景1] new_message 后快照:', JSON.stringify(snap, null, 2))

    // ★ 断言1：消息存在且 completed
    expect(snap.found).toBe(true)
    expect(snap.status).toBe('completed')

    // ★ 断言2：tool_call part 完整保留（工具卡片必须显示）
    const toolParts = snap.parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('tc-1')
    expect(toolParts[0].name).toBe('search')
    expect(toolParts[0].result).toBe('搜索结果1')

    // ★ 断言3：两轮正文都保留
    const textParts = snap.parts.filter((p: any) => p.type === 'text')
    const allText = textParts.map((p: any) => p.content).join('')
    expect(allText).toContain('第一轮正文')
    expect(allText).toContain('基于工具结果的回答')

    // ★ 断言4：思考保留
    const thinkParts = snap.parts.filter((p: any) => p.type === 'thinking')
    expect(thinkParts.length).toBe(1)
    expect(thinkParts[0].content).toContain('第一轮思考')
  })

  it('场景2（RAF 未 flush 时 new_message 到达）：本地累积的 tool_call 不被 serverParts 覆盖', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 正文 delta 缓冲（不推进 RAF）
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'text' }))
    handleTextDelta(makeEvent('text_delta', { index: 0, text: '正文', sequence: 2 }))

    // 工具调用（同步 handler，不依赖 RAF）
    handleToolStart(makeEvent('tool_start', {
      call_id: 'tc-1', tool_name: 'search', args: {}, sequence: 3,
    }))
    handleToolResult(makeEvent('tool_result', {
      call_id: 'tc-1', tool_name: 'search', result: '结果', success: true,
    }))

    // 不推进 RAF，直接 new_message 到达（镜像真实主线程时序：引擎完成瞬间 new_message 先于 RAF flush）
    handleNewMessage(makeEvent('new_message', {
      content: '正文',
      parts: [{ type: 'text', text: '正文' }],
      sequence: 4,
    }))

    const snap = snapshotMessage()
    console.log('[场景2] new_message 后快照:', JSON.stringify(snap, null, 2))

    // ★ 断言：tool_call part 不丢失（工具卡片必须显示）
    const toolParts = snap.parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('tc-1')
    expect(toolParts[0].result).toBe('结果')
  })

  it('场景3（tool_start 乱序/占位丢失）：tool_start 到达时消息不存在，工具调用不静默丢弃', async () => {
    // 模拟 stream_start 占位丢失（WS 断线/乱序），tool_start 先到
    handleToolStart(makeEvent('tool_start', {
      call_id: 'tc-1', tool_name: 'search', args: {}, sequence: 1,
    }))

    const snap = snapshotMessage()
    console.log('[场景3] tool_start 后快照:', JSON.stringify(snap, null, 2))

    // ★ 断言：工具调用不静默丢弃（消息占位存在，含 tool_call part）
    expect(snap.found).toBe(true)
    const toolParts = snap.parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('tc-1')
  })

  it('场景4（tool_result 乱序）：tool_result 先于 tool_start 到达，工具结果不静默丢弃', async () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // tool_result 先到（tool_start 事件丢失/乱序），bridge_events 有 FIXUP 自动补发 tool_start
    handleToolResult(makeEvent('tool_result', {
      call_id: 'tc-1', tool_name: 'search', result: '结果', success: true,
    }))

    const snap = snapshotMessage()
    console.log('[场景4] tool_result 后快照:', JSON.stringify(snap, null, 2))

    // ★ 断言：工具结果不静默丢弃（消息占位存在，tool_call part 状态 done 且含结果）
    expect(snap.found).toBe(true)
    const toolParts = snap.parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('tc-1')
    expect(toolParts[0].result).toBe('结果')
  })
})
