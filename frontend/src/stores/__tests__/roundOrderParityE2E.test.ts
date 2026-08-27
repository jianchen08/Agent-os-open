/**
 * 聊天消息顺序对等（CI 门禁）：一轮 = 一条消息（DSH 形态）的事件流 → store
 * 最终顺序必须与后端 message_slots 的 seq 升序完全一致（后端顺序不变式）。
 *
 * 场景：2 轮 agent 循环（第1轮带工具调用），逐轮发 stream_start → 块增量 →
 * 工具事件 → new_message → stream_end。断言：
 * 1. store（渲染序 = compareMessages）与后端 seq 序对等——流式期间与结束后
 *    同一顺序（这是「顺序混乱」修复的回归锚）；
 * 2. new_message 落地的 authoritiative content/sequence 与后端逐轮记录一致；
 * 3. 渲染层合并（mergeConsecutiveAssistantMessages）保持多轮工具调用气泡
 *    连续性：一轮辅助消息（text→tool→text）合并为单个气泡，不拆不串。
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

describe('逐轮模型顺序对等（后端 seq 序 vs 前端渲染序）', () => {
  const PIPELINE_ID = '39ef1314a7b9000000000000'
  const THREAD_ID = 'thread-parity-001'
  const MSG1 = 'a_' + '1'.repeat(32)
  const MSG2 = 'a_' + '2'.repeat(32)
  const CALL1 = 'call_r1_01'

  let pipelineStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let handlers: typeof import('@/services/websocket/streaming/handlers')

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
    handlers = await import('@/services/websocket/streaming/handlers')
    pipelineStore.getState().registerPipeline({ pipelineId: PIPELINE_ID, sessionId: THREAD_ID })
    pipelineStore.getState().initFromAPI(PIPELINE_ID, [
      { id: 'user-1', sessionId: THREAD_ID, role: 'user', content: '问1', sequence: 0, timestamp: new Date().toISOString(), parentId: null, status: 'completed' } as any,
    ])
  })

  const ev = (type: string, payload: Record<string, unknown>) => ({ type, ...payload })

  const flush = () => new Promise((r) => setTimeout(r, 20))

  it('两轮循环（第1轮带工具）最终顺序 == 后端 seq 序，渲染合并单气泡连续', async () => {
    // ── 第1轮：文本 → 工具 → new_message → stream_end ──
    handlers.handleStreamStart(ev('stream_start', { pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID }))
    handlers.handleTextDelta(ev('text_delta', { pipeline_id: PIPELINE_ID, message_id: MSG1, index: 0, text: '第一轮前' }))
    await flush()
    handlers.handleToolStart(ev('tool_start', {
      pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID,
      call_id: CALL1, tool_name: 'file_read', args: { path: '/tmp/a' },
    }))
    handlers.handleToolResult(ev('tool_result', {
      pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID,
      call_id: CALL1, tool_name: 'file_read', result: '文件内容', success: true,
    }))
    handlers.handleNewMessage(ev('new_message', {
      pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID,
        sequence: 1,
        message: {
          id: MSG1, role: 'assistant', content: '第一轮前', sequence: 1,
          status: 'completed', thread_id: THREAD_ID,
          toolCalls: [{ id: CALL1, name: 'file_read', arguments: '{"path":"/tmp/a"}' }],
        },
      },
    }))
    handlers.handleStreamEnd(ev('stream_end', { pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID, data: { final_sequence: 1 } }))

    // ── 第2轮：纯文本回绕 ──
    handlers.handleStreamStart(ev('stream_start', { pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID }))
    handlers.handleTextDelta(ev('text_delta', { pipeline_id: PIPELINE_ID, message_id: MSG2, index: 0, text: '第二轮回复' }))
    await flush()
    handlers.handleNewMessage(ev('new_message', {
      pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID,
        sequence: 2,
        message: {
          id: MSG2, role: 'assistant', content: '第二轮回复', sequence: 2,
          status: 'completed', thread_id: THREAD_ID,
        },
      },
    }))
    handlers.handleStreamEnd(ev('stream_end', { pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID, data: { final_sequence: 2 } }))

    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)

    // ── ① 顺序对等：前端渲染序（compareMessages = sequence→timestamp→id）== 后端 seq 序 ──
    const { compareMessages } = await import('@/utils/messageOrder')
    const orderedIds = [...msgs].sort(compareMessages).map((m) => m.id as string)
    expect(orderedIds).toEqual(['user-1', MSG1, MSG2])
    // 每条 authoritative sequence 与后端一致（1/2），且与排序位置一致
    const seqOf = (id: string) => msgs.find((m) => m.id === id)?.sequence
    expect(seqOf(MSG1)).toBe(1)
    expect(seqOf(MSG2)).toBe(2)
    expect([...msgs].sort(compareMessages).every((m, i) => m.sequence === (i === 0 ? 0 : m.sequence))).toBe(true)

    // ── ② 每轮内容不串台 ──
    const m1 = msgs.find((m) => m.id === MSG1)!
    const m2 = msgs.find((m) => m.id === MSG2)!
    expect((m1.content || '') + m1.parts?.map((p: any) => p.content || '').join('')).toContain('第一轮前')
    expect((m2.content || '') + m2.parts?.map((p: any) => p.content || '').join('')).toContain('第二轮回复')
    expect(m1.content).not.toContain('第二轮')

    // ── ③ 渲染层合并：多轮工具调用气泡连续性（assistant+tool 交错序列 = 单个气泡，
    //    与流式期同构——流式时 tool 卡挂同一占位；数据层仍逐轮独立保留）──
    const { mergeConsecutiveAssistantMessages } = await import('@/services/api/session')
    const merged = mergeConsecutiveAssistantMessages([...msgs].sort(compareMessages))
    const assistantBubbles = merged.filter((m) => m.role === 'assistant')
    expect(assistantBubbles.length).toBe(1) // 一轮多轮工具调用 = 一个连续气泡
    const bubble = assistantBubbles[0]
    const bubbleParts = bubble.parts as any[]
    expect(bubbleParts.filter((p) => p.type === 'text').map((p) => p.content).join('')).toContain('第一轮前')
    expect(bubbleParts.filter((p) => p.type === 'text').map((p) => p.content).join('')).toContain('第二轮回复')
    const toolPart = bubbleParts.find((p) => p.type === 'tool_call')
    expect(toolPart).toBeDefined()
    expect(toolPart.callId).toBe(CALL1)
    // 数据层不被合并吞掉：每条消息仍在（顺序对等锚）
    expect(msgs.map((m) => m.id)).toEqual(['user-1', MSG1, MSG2])
  })

  it('回归锚：round1 终态迟到于 round2 stream_start，顺序仍按 seq 对等', async () => {
    handlers.handleStreamStart(ev('stream_start', { pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID }))
    handlers.handleTextDelta(ev('text_delta', { pipeline_id: PIPELINE_ID, message_id: MSG1, index: 0, text: '第一轮回答' }))
    await flush()

    // 第2轮 start 先到（第1轮终态还在路上）
    handlers.handleStreamStart(ev('stream_start', { pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID }))
    handlers.handleTextDelta(ev('text_delta', { pipeline_id: PIPELINE_ID, message_id: MSG2, index: 0, text: '第二轮回答' }))
    await flush()

    // 第1轮终态补到（new_message + stream_end）
    handlers.handleNewMessage(ev('new_message', {
      pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID, sequence: 1,
        message: { id: MSG1, role: 'assistant', content: '第一轮回答', sequence: 1, status: 'completed', thread_id: THREAD_ID },
      },
    }))
    handlers.handleStreamEnd(ev('stream_end', { pipeline_id: PIPELINE_ID, message_id: MSG1, _threadId: THREAD_ID, data: { final_sequence: 1 } }))
    // 第2轮终态
    handlers.handleNewMessage(ev('new_message', {
      pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID, sequence: 2,
        message: { id: MSG2, role: 'assistant', content: '第二轮回答', sequence: 2, status: 'completed', thread_id: THREAD_ID },
      },
    }))
    handlers.handleStreamEnd(ev('stream_end', { pipeline_id: PIPELINE_ID, message_id: MSG2, _threadId: THREAD_ID, data: { final_sequence: 2 } }))

    const { compareMessages } = await import('@/utils/messageOrder')
    const msgs = pipelineStore.getState().getMessages(PIPELINE_ID)
    const orderedIds = [...msgs].sort(compareMessages).map((m) => m.id as string)
    expect(orderedIds).toEqual(['user-1', MSG1, MSG2])
    const m1 = msgs.find((m) => m.id === MSG1)!
    expect(m1.status).toBe('completed')
    expect(m1.sequence).toBe(1)
  })
})
