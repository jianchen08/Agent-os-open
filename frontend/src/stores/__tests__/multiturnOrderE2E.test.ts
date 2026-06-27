/**
 * 多轮顺序 E2E：验证「后端推送序 → 前端接收序 → 渲染序」三者一致。
 *
 * 策略：用真实 WS 事件序列喂给真实 handlers（不 mock pipelineStore），
 * 验证 store 中消息与 parts 的顺序，再用 useMessageRender 验证渲染片段顺序。
 *
 * 覆盖场景（与真实后端 e2e_ws_multiturn_order.py 抓取的序列一致）：
 * 1. 单轮 user → AI文本 → 工具 → AI文本：parts 顺序 [text, tool_call, text]
 * 2. 连发两轮：两轮各自独立 message_id；消息间按 sequence 排序
 * 3. 延迟终态：第1轮的 new_message/stream_end 在第2轮 stream_start 之前才到达
 *    （后端真实时序，见 state_change=suspended 后才补发 new_message）
 */
import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useMessageRender } from '@/components/chat/hooks/useMessageRender'
import type { Message } from '@/types/models'

// ── mock 外部依赖（与 MessageOrderVerification.test.tsx 对齐）──
vi.mock('@/utils/activityConverter', () => ({
  toolCallToActivity: (toolCall: any) => ({
    type: 'tool_call',
    id: toolCall.callId ?? toolCall.call_id,
    title: toolCall.name ?? toolCall.tool_name,
    toolName: toolCall.name ?? toolCall.tool_name,
    status: toolCall.state ?? toolCall.status ?? 'pending',
    details: [],
    actions: [],
  }),
}))
vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (base: any) => base,
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
vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))
vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pid_a00000000000'
const THREAD_ID = 'tid_b00000000000'

let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
let handlers: typeof import('@/services/websocket/streaming/handlers')
let flushStreamChunkBuffer: typeof import('@/services/websocket/streaming/handlers/streamHandler').flushStreamChunkBuffer

/** 构造一个 WS 事件（顶层 + data 双层字段，匹配真实后端 _make_event 形态） */
/**
 * 单调递增的 part sequence 计数器，模拟后端 _next_part_seq()。
 *
 * 真实后端 bridge_events 给每个流式事件（stream_chunk/tool_start/...）分配递增
 * sequence，前端 useMessageRender 据此对 parts 排序。若 mock 事件不带 sequence，
 * 文本 part 会落到 sequence=Date.now()（巨数），与 tool_call 的小 sequence 冲突，
 * 导致渲染顺序错乱——这是测试保真度问题，不是产品 bug。
 */
let _partSeq = 0
function nextSeq(): number {
  _partSeq += 1
  return _partSeq
}

function evt(type: string, data: Record<string, any>): any {
  // 未显式给 sequence 时自动分配递增值（与真实后端一致）
  const seq = data.sequence ?? nextSeq()
  return {
    type,
    sequence: seq,
    data: { pipeline_id: PIPELINE_ID, message_id: data.message_id, sequence: seq, ...data },
  }
}

/**
 * 刷写 streamChunk 的 RAF 缓冲。
 *
 * 真实浏览器里 requestAnimationFrame 每帧触发 _flushChunks，把缓冲的 chunk 写入 store。
 * jsdom 不自动跑 RAF，需手动调用 flushStreamChunkBuffer 模拟"一帧过去"，
 * 否则 chunk 悬在缓冲区、store 看不到文本 part，渲染顺序断言会失真。
 */
function flush(): void {
  flushStreamChunkBuffer()
}

/** 读取某消息的渲染片段类型序列 */
function fragmentTypesOf(message: Message): string[] {
  const { result } = renderHook(() => useMessageRender({ message }))
  return result.current.fragments.map((f: any) => f.type)
}

beforeEach(async () => {
  vi.resetModules()
  _partSeq = 0 // 每个用例重置 part sequence 计数器
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
  const h = await import('@/services/websocket/streaming/handlers')
  handlers = h
  const sf = await import('@/services/websocket/streaming/handlers/streamHandler')
  flushStreamChunkBuffer = sf.flushStreamChunkBuffer

  pipelineStore.getState().registerPipeline({ pipelineId: PIPELINE_ID, sessionId: THREAD_ID })
  pipelineStore.getState().activatePipeline(PIPELINE_ID)
})

describe('多轮顺序 E2E：推送序 → 接收序 → 渲染序', () => {
  it('单轮：AI文本 → 工具 → AI文本 的 parts 渲染顺序正确', () => {
    const MSG = 'msg_round1_a0000000'
    const CALL = 'call_r1_001'

    // 用户消息（手动加入，模拟乐观更新）
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'user-1', sessionId: THREAD_ID, role: 'user', content: '帮我读文件',
      sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed',
    } as any)

    // 后端事件序列：stream_start → chunk(前文本) → tool_start → tool_result → chunk(后文本) → stream_end(parts)
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG, _threadId: THREAD_ID, sequence: 2 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG, content: '好的，' }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG, content: '我来读取。' }))
    flush() // 模拟一帧 RAF：前文本 chunk 落盘为 text part（否则 tool_start 前无文本片段）
    handlers.handleToolStart(evt('tool_start', {
      message_id: MSG, tool_name: 'file_read', call_id: CALL, args: { path: '/tmp/x' },
    }))
    handlers.handleToolResult(evt('tool_result', {
      message_id: MSG, tool_name: 'file_read', call_id: CALL, success: true, result: '内容', duration_ms: 5,
    }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG, content: '读取完成。' }))
    flush() // 后文本 chunk 落盘为新 text part（被 tool_start 关闭后另起一段）

    // stream_end 携带后端权威 parts（camelCase 子项，来自 _build_parts_from_state）
    handlers.handleStreamEnd(evt('stream_end', {
      message_id: MSG, _threadId: THREAD_ID, final_sequence: 2,
      full_content: '好的，我来读取。读取完成。',
      parts: [
        { type: 'tool_call', callId: CALL, name: 'file_read', args: { path: '/tmp/x' }, state: 'done', result: '内容', sequence: 3 },
      ],
    }))

    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ai = msgs.find((m) => m.id === MSG)!

    // 渲染片段顺序：text → tool_call → text（tool_call 前后各一段文本）
    const types = fragmentTypesOf(ai)
    expect(types).toEqual(['text', 'tool_call', 'text'])
    // 工具 part 状态为 done 且带 result
    const toolPart = (ai.parts || []).find((p: any) => p.type === 'tool_call')!
    expect(toolPart.state).toBe('done')
    expect(toolPart.result).toBe('内容')
  })

  it('连发两轮：两轮 message_id 独立，消息间按到达顺序排列', () => {
    const MSG1 = 'msg_round1_b0000000'
    const MSG2 = 'msg_round2_b0000000'

    // user-1 先发（第1轮提问）
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'user-1', sessionId: THREAD_ID, role: 'user', content: '第一问',
      sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed',
    } as any)

    // ── 第1轮 ──（store 渲染顺序=插入顺序，非 sequence 排序，故按真实对话时序插入）
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG1, _threadId: THREAD_ID, sequence: 2 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG1, content: '第一轮回答' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', { message_id: MSG1, _threadId: THREAD_ID, final_sequence: 2, full_content: '第一轮回答', parts: [{ type: 'text', content: '第一轮回答', state: 'done' }] }))

    // user-2 在第1轮回答后才发（第2轮提问）—— 真实对话时序
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'user-2', sessionId: THREAD_ID, role: 'user', content: '第二问',
      sequence: 3, timestamp: new Date().toISOString(), parentId: null, status: 'completed',
    } as any)

    // ── 第2轮 ──
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG2, _threadId: THREAD_ID, sequence: 4 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG2, content: '第二轮回答' }))
    flush()
    handlers.handleStreamEnd(evt('stream_end', { message_id: MSG2, _threadId: THREAD_ID, final_sequence: 4, full_content: '第二轮回答', parts: [{ type: 'text', content: '第二轮回答', state: 'done' }] }))

    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = msgs.map((m) => m.id)
    // 顺序：user-1 → MSG1 → user-2 → MSG2（按真实对话到达时序）
    expect(ids).toEqual(['user-1', MSG1, 'user-2', MSG2])
    // 两轮 message_id 不同（各自独立 AI 消息）
    expect(MSG1).not.toBe(MSG2)
    // 两条 AI 消息都 completed
    expect(msgs.find((m) => m.id === MSG1)!.status).toBe('completed')
    expect(msgs.find((m) => m.id === MSG2)!.status).toBe('completed')
  })

  it('延迟终态：第1轮 stream_end 在第2轮 stream_start 之后才到，顺序仍正确', () => {
    // 还原真实后端时序：state_change=suspended 后用户即发第2问，第2轮已 stream_start，
    // 然后第1轮的 new_message/stream_end 才补到 —— 验证两条 AI 消息不互相破坏。
    const MSG1 = 'msg_round1_c0000000'
    const MSG2 = 'msg_round2_c0000000'

    // user-1（第1轮提问）
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'user-1', sessionId: THREAD_ID, role: 'user', content: '第一问',
      sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed',
    } as any)

    // 第1轮：start + chunk（此时 MSG1 仍 streaming，stream_end 尚未到）
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG1, _threadId: THREAD_ID, sequence: 2 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG1, content: '第一轮回答' }))
    flush() // 让 MSG1 有内容，第2轮 stream_start 的旧占位清理才会"保留(completed)"而非"删除"

    // user-2 在第1轮结束后才发（真实对话时序，state_change=suspended 后用户发新消息）
    pipelineStore.getState().addMessage(PIPELINE_ID, {
      id: 'user-2', sessionId: THREAD_ID, role: 'user', content: '第二问',
      sequence: 3, timestamp: new Date().toISOString(), parentId: null, status: 'completed',
    } as any)

    // 第2轮：start + chunk（MSG2 开始，MSG1 尚未 stream_end）
    handlers.handleStreamStart(evt('stream_start', { message_id: MSG2, _threadId: THREAD_ID, sequence: 4 }))
    handlers.handleStreamChunk(evt('stream_chunk', { message_id: MSG2, content: '第二轮回答' }))
    flush()

    // 第1轮的延迟 stream_end 才补到（真实后端会带 parts + full_content，来自 _build_parts_from_state）
    handlers.handleStreamEnd(evt('stream_end', { message_id: MSG1, _threadId: THREAD_ID, final_sequence: 2, full_content: '第一轮回答', parts: [{ type: 'text', content: '第一轮回答', state: 'done' }] }))
    // 第2轮 stream_end
    handlers.handleStreamEnd(evt('stream_end', { message_id: MSG2, _threadId: THREAD_ID, final_sequence: 4, full_content: '第二轮回答', parts: [{ type: 'text', content: '第二轮回答', state: 'done' }] }))

    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = msgs.map((m) => m.id)
    // 顺序仍是 user-1 → MSG1 → user-2 → MSG2（按真实到达时序）
    expect(ids).toEqual(['user-1', MSG1, 'user-2', MSG2])
    // 两条 AI 都 completed 且各自内容正确（不串台）
    const m1 = msgs.find((m) => m.id === MSG1)!
    const m2 = msgs.find((m) => m.id === MSG2)!
    expect(m1.status).toBe('completed')
    expect(m2.status).toBe('completed')
    expect(m1.content).toContain('第一轮')
    expect(m2.content).toContain('第二轮')
  })

  it('两轮各自含工具：每轮 parts 顺序 [text, tool_call, text]，互不混淆', () => {
    const MSG1 = 'msg_round1_d0000000'
    const MSG2 = 'msg_round2_d0000000'
    const CALL1 = 'call_r1_x01'
    const CALL2 = 'call_r2_x02'

    const runRound = (msg: string, callId: string, text1: string, text2: string, msgSeq: number) => {
      handlers.handleStreamStart(evt('stream_start', { message_id: msg, _threadId: THREAD_ID, sequence: msgSeq }))
      handlers.handleStreamChunk(evt('stream_chunk', { message_id: msg, content: text1 }))
      flush() // 前文本落盘
      handlers.handleToolStart(evt('tool_start', {
        message_id: msg, tool_name: 'file_read', call_id: callId, args: { path: '/tmp/a' },
      }))
      handlers.handleToolResult(evt('tool_result', {
        message_id: msg, tool_name: 'file_read', call_id: callId, success: true, result: 'ok', duration_ms: 3,
      }))
      handlers.handleStreamChunk(evt('stream_chunk', { message_id: msg, content: text2 }))
      flush() // 后文本落盘（tool_start 已关闭前 text part，此 chunk 另起一段）
      handlers.handleStreamEnd(evt('stream_end', {
        message_id: msg, _threadId: THREAD_ID, final_sequence: msgSeq,
        full_content: `${text1}${text2}`,
        // server parts 仅作兜底，本地 parts 已含完整 text+tool_call，mergeStreamingParts 会保留本地
        parts: [{ type: 'tool_call', callId, name: 'file_read', args: { path: '/tmp/a' }, state: 'done', result: 'ok' }],
      }))
    }

    // user-1（第1轮提问）
    pipelineStore.getState().addMessage(PIPELINE_ID, { id: 'user-1', sessionId: THREAD_ID, role: 'user', content: '问1', sequence: 1, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as any)

    runRound(MSG1, CALL1, '第一轮前', '第一轮后', 2)

    // user-2 在第1轮回答后才发（真实对话时序）
    pipelineStore.getState().addMessage(PIPELINE_ID, { id: 'user-2', sessionId: THREAD_ID, role: 'user', content: '问2', sequence: 3, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as any)

    runRound(MSG2, CALL2, '第二轮前', '第二轮后', 4)

    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const ids = msgs.map((m) => m.id)
    // 消息整体顺序：user-1 → MSG1 → user-2 → MSG2（按真实对话到达时序）
    expect(ids).toEqual(['user-1', MSG1, 'user-2', MSG2])
    const m1 = msgs.find((m) => m.id === MSG1)!
    const m2 = msgs.find((m) => m.id === MSG2)!

    // 每轮渲染片段：text → tool_call → text
    expect(fragmentTypesOf(m1)).toEqual(['text', 'tool_call', 'text'])
    expect(fragmentTypesOf(m2)).toEqual(['text', 'tool_call', 'text'])
    // 两个 tool_call 的 callId 不同（各自的工具调用）
    const tc1 = (m1.parts || []).find((p: any) => p.type === 'tool_call')!
    const tc2 = (m2.parts || []).find((p: any) => p.type === 'tool_call')!
    expect(tc1.callId).toBe(CALL1)
    expect(tc2.callId).toBe(CALL2)
    expect(tc1.callId).not.toBe(tc2.callId)
    // 文本内容不串台
    expect(m1.content).toContain('第一轮前')
    expect(m1.content).toContain('第一轮后')
    expect(m2.content).toContain('第二轮前')
    expect(m2.content).toContain('第二轮后')
  })
})
