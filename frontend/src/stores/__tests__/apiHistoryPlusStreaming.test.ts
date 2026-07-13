/**
 * API 历史 + 实时流式 合并测试：验证顺序、渲染、气泡三者正确。
 *
 * 用户场景：打开会话（API 加载历史）→ 发消息 → 收到流式回复（含工具）→
 *           切走再切回（补漏）。验证：
 * 1. 历史消息 + 流式消息合并后整体顺序正确（user→ai→user→ai 交错，不乱序）
 * 2. 每条消息的渲染片段顺序正确（text→tool_call→text）
 * 3. 无空气泡（completed 的 assistant 消息必须有 content 或 parts）
 * 4. 无重复消息 / 无重复 text part（渲染层防线）
 *
 * 策略：mock apiClient.get（网络层），让 fetchMessages + mapBackendMessageToMessage +
 *      mergeConsecutiveAssistantMessages 真实跑；WS 事件喂真实 handlers。
 *      与 renderLayerNoDuplicateParts / dualCursorBackfillIntegration 同模式。
 */
import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMessageRender } from '@/components/chat/hooks/useMessageRender'
import type { Message } from '@/types/models'

// ── mock apiClient（网络层），保留真实的 map + merge ──
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/utils/activityConverter', () => ({
  toolCallToActivity: (tc: any) => ({
    type: 'tool_call', id: tc.callId ?? tc.call_id,
    title: tc.name ?? tc.tool_name, toolName: tc.name ?? tc.tool_name,
    status: tc.state ?? tc.status ?? 'pending', details: [], actions: [],
  }),
}))
vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (b: any) => b,
  getToolCardConfig: () => null,
}))
vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))
vi.mock('@/utils/retry', () => ({
  requestWithRetry: async (fn: () => Promise<any>) => fn(),
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pipe-merge-001'
const THREAD_ID = 'thread-merge-001'

let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
let handlers: typeof import('@/services/websocket/streaming/handlers')
let flushStreamChunkBuffer: typeof import('@/services/websocket/streaming/handlers/streamHandler').flushStreamChunkBuffer

/** part sequence 计数器（模拟后端 _next_part_seq） */
let _partSeq = 0
const nextSeq = () => ++_partSeq

/** 构造 WS 事件（顶层 + data 双层，自动分配递增 part sequence） */
function evt(type: string, data: Record<string, any>): any {
  const seq = data.sequence ?? nextSeq()
  return { type, sequence: seq, data: { pipeline_id: PIPELINE_ID, message_id: data.message_id, sequence: seq, ...data } }
}

/** 刷写 streamChunk RAF 缓冲（jsdom 不自动跑 RAF） */
function flush(): void {
  flushStreamChunkBuffer()
}

/** 设置 apiClient.get 返回的后端原始 records */
function setApiRecords(records: any[], hasMore = false) {
  mockGet.mockResolvedValueOnce({ data: { messages: records, total: records.length, has_more: hasMore } })
}

/** 取某消息的渲染片段类型序列 */
function fragmentTypes(msg: Message): string[] {
  const { result } = renderHook(() => useMessageRender({ message: msg }))
  return result.current.fragments.map((f: any) => f.type)
}

beforeEach(async () => {
  vi.clearAllMocks()
  vi.resetModules()
  _partSeq = 0
  const storeMod = await import('@/stores/pipelineMessageStore')
  pipelineStore = storeMod.usePipelineMessageStore
  pipelineStore.setState({
    messagesByPipeline: {}, pipelines: {},
    pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
    streamingState: {}, activePipelineId: null,
    topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
  })
  mockGet.mockReset()
  const h = await import('@/services/websocket/streaming/handlers')
  handlers = h
  const sf = await import('@/services/websocket/streaming/handlers/streamHandler')
  flushStreamChunkBuffer = sf.flushStreamChunkBuffer

  pipelineStore.getState().registerPipeline({
    pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
    agentName: '', status: 'idle', parentId: null, unreadCount: 0,
  })
  pipelineStore.getState().activatePipeline(PIPELINE_ID)
})

describe('API 历史 + 实时流式 合并：顺序 / 渲染 / 气泡', () => {
  it('历史[3条] + 流式[含工具1轮] 合并后顺序与渲染正确，无空气泡无重复', async () => {
    // ── 1. API 加载历史：user1 → ai1(含工具) ──
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '历史问题' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '',
        toolCalls: [{ call_id: 'tc-0', tool_name: 'file_read', tool_args: { path: '/h' }, result: '历史结果' }] },
      { id: 'a1-text', sequence: 3, role: 'assistant', content: '历史回复完成' },
    ])
    await pipelineStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // 历史加载后：user1 + ai1（tool_call + text 合并到一条）
    const afterHistory = pipelineStore.getState().getMessages(PIPELINE_ID)
    expect(afterHistory.some((m) => m.id === 'u1' && m.role === 'user')).toBe(true)
    const histAi = afterHistory.find((m) => m.role === 'assistant')
    expect(histAi).toBeDefined()
    // 历史气泡非空：有 tool_call part 或 text content
    const histHasContent = !!(histAi!.content?.trim()) || (histAi!.parts || []).length > 0
    expect(histHasContent, '历史 AI 消息不应是空气泡').toBe(true)

    // ── 2. 用户发新消息（乐观） + 流式回复（含工具）──
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'u2-local', sessionId: THREAD_ID, role: 'user', content: '新问题',
      sequence: 4, timestamp: new Date().toISOString(), parentId: null,
      status: 'completed', clientMessageId: 'u2-local',
    } as any)

    const STREAM_MSG = 'msg_stream_a0000000'
    const STREAM_CALL = 'call_stream_01'
    handlers.handleStreamStart(evt('stream_start', { message_id: STREAM_MSG, _threadId: THREAD_ID, sequence: 5 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: STREAM_MSG, content: '好的，' }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: STREAM_MSG, content: '我来查。' }))
    flush()
    handlers.handleToolStart(evt('tool_start', {
      message_id: STREAM_MSG, tool_name: 'file_read', call_id: STREAM_CALL, args: { path: '/n' },
    }))
    handlers.handleToolResult(evt('tool_result', {
      message_id: STREAM_MSG, tool_name: 'file_read', call_id: STREAM_CALL, success: true, result: '新结果', duration_ms: 4,
    }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: STREAM_MSG, content: '查询完成。' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: STREAM_MSG, _threadId: THREAD_ID, final_sequence: 5,
      full_content: '好的，我来查。查询完成。',
      parts: [{ type: 'tool_call', callId: STREAM_CALL, name: 'file_read', args: { path: '/n' }, state: 'done', result: '新结果' }],
    }))

    // ── 3. 断言最终合并状态 ──
    const final = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = final.map((m) => m.id)

    // 3a 无重复 id
    expect(new Set(ids).size, '无重复消息 id').toBe(ids.length)

    // 3b 顺序：历史 user/ai 在前，新 user/流式 ai 在后（按到达时序）
    const u1Idx = ids.indexOf('u1')
    const u2Idx = ids.indexOf('u2-local')
    const streamIdx = ids.indexOf(STREAM_MSG)
    expect(u1Idx, '历史 user1 在最前').toBe(0)
    expect(u2Idx, '新 user 在历史之后').toBeGreaterThan(u1Idx)
    expect(streamIdx, '流式 AI 在新 user 之后').toBeGreaterThan(u2Idx)

    // 3c 无空气泡：所有 completed assistant 都有 content 或 parts
    for (const m of final) {
      if (m.role !== 'assistant' || m.status === 'streaming') continue
      const ok = !!(m.content?.trim()) || (m.parts || []).length > 0
      expect(ok, `assistant 消息 ${m.id} 是空气泡`).toBe(true)
    }

    // 3d 流式消息渲染片段顺序：text → tool_call → text
    const streamMsg = final.find((m) => m.id === STREAM_MSG)!
    expect(fragmentTypes(streamMsg), '流式消息片段顺序').toEqual(['text', 'tool_call', 'text'])

    // 3e 无重复 text part（渲染层防线）
    for (const m of final) {
      const textParts = (m.parts || []).filter((p: any) => p.type === 'text')
      const contents = textParts.map((p: any) => (p.content || '').trim()).filter(Boolean)
      const seen = new Set<string>()
      for (const c of contents) {
        expect(seen.has(c), `消息 ${m.id} 有重复 text part: ${c.slice(0, 20)}`).toBe(false)
        seen.add(c)
      }
    }
  })

  it('历史加载后切走再切回补漏：历史 + 补漏 + 流式 三者合并无重复无空气泡', async () => {
    const store = pipelineStore.getState()

    // ── 首次 init：历史一轮 ──
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题1' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '回复1' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    const bottomCursor = pipelineStore.getState().getBottomCursor(PIPELINE_ID)

    // ── 流式一轮（切走期间发生）──
    const STREAM_MSG = 'msg_backfill_stream'
    handlers.handleStreamStart(evt('stream_start', { message_id: STREAM_MSG, _threadId: THREAD_ID, sequence: 4 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: STREAM_MSG, content: '流式回复' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: STREAM_MSG, _threadId: THREAD_ID, final_sequence: 4,
      full_content: '流式回复', parts: [{ type: 'text', content: '流式回复', state: 'done' }],
    }))

    // ── 切回：补漏（API 返回流式期间持久化的消息）──
    setApiRecords([
      { id: 'u2', sequence: 3, role: 'user', content: '问题2' },
      { id: STREAM_MSG, sequence: 4, role: 'assistant', content: '流式回复' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: bottomCursor })

    // ── 断言：补漏后流式消息（同 id）不重复，顺序正确，无空气泡 ──
    const final = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = final.map((m) => m.id)

    // 流式消息只有 1 条（补漏的同 id 被去重，不产生重复气泡）
    const streamCount = ids.filter((id) => id === STREAM_MSG).length
    expect(streamCount, '流式消息补漏后不重复').toBe(1)
    // 整体无重复 id
    expect(new Set(ids).size).toBe(ids.length)
    // 顺序按 sequence：u1(1) a1(2) u2(3) STREAM(4)
    const orderedBySeq = [...final].sort((a, b) => (a.sequence ?? 0) - (b.sequence ?? 0))
    expect(orderedBySeq.map((m) => m.sequence)).toEqual([1, 2, 3, 4])
    // 无空气泡
    for (const m of final) {
      if (m.role !== 'assistant' || m.status === 'streaming') continue
      const ok = !!(m.content?.trim()) || (m.parts || []).length > 0
      expect(ok, `assistant 消息 ${m.id} 空气泡`).toBe(true)
    }
  })

  it('流式回复完成后 reload（initFromAPI 覆盖）：流式消息被 API 版本替换，不重复', async () => {
    // 这是"刷新页面"场景：流式消息已在内存，reload 从 API 重新加载
    const store = pipelineStore.getState()

    // 流式一轮
    const STREAM_MSG = 'msg_reload_stream'
    handlers.handleStreamStart(evt('stream_start', { message_id: STREAM_MSG, _threadId: THREAD_ID, sequence: 2 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: STREAM_MSG, content: '刷新前回复' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: STREAM_MSG, _threadId: THREAD_ID, final_sequence: 2,
      full_content: '刷新前回复', parts: [{ type: 'text', content: '刷新前回复', state: 'done' }],
    }))

    // reload：API 返回完整历史（含已完成的流式消息）
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题' },
      { id: STREAM_MSG, sequence: 2, role: 'assistant', content: '刷新前回复' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const final = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = final.map((m) => m.id)

    // 流式消息不重复（同 id 被合并为 1 条）
    expect(ids.filter((id) => id === STREAM_MSG).length, 'reload 后流式消息不重复').toBe(1)
    expect(new Set(ids).size).toBe(ids.length)
    // 内容正确（API 版本）
    const m = final.find((x) => x.id === STREAM_MSG)!
    expect(m.content).toBe('刷新前回复')
    // 无空气泡
    for (const msg of final) {
      if (msg.role !== 'assistant' || msg.status === 'streaming') continue
      const ok = !!(msg.content?.trim()) || (msg.parts || []).length > 0
      expect(ok, `assistant 消息 ${msg.id} 空气泡`).toBe(true)
    }
  })
})
