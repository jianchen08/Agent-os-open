/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * new_message 完整消息形态（data.message）测试——冷热路径同构（A2）。
 *
 * 后端 ws_session.rs 的 new_message 现在携带与 DB 加载同构的完整消息
 * （content/reasoningContent/toolCalls/sequence），前端经共享 mapper
 * （mapBackendMessageToMessage）生成 parts，与历史加载同一套逻辑。
 *
 * 核心断言：
 *   1. data.message 的 thinking/tool_calls/text 被 mapper 还原为 parts
 *     （思考卡片、工具卡片、正文与刷新后渲染一致）
 *   2. 本地已累积的流式 parts 不被覆盖（mergeStreamingParts 本地优先）
 *   3. 流式中断（本地无内容）时 data.message 兜底填充完整形态
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api/session')>()
  return {
    ...actual,
    getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  }
})

const PIPELINE_ID = 'pipe-newmsg-shape-001'
const MESSAGE_ID = 'msg_newmsg_shape_01'
const THREAD_ID = 'thread-newmsg-shape-001'

/** 构造后端 WS 事件信封（与 ws_session.rs 一致：业务字段在 data 下） */
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

/** 后端落库消息的完整形态（与 session_routes.rs 投影 + ws_session new_message.message 同构） */
function makeServerMessage(overrides: Record<string, any> = {}) {
  return {
    id: MESSAGE_ID,
    role: 'assistant',
    content: '最终回答文本',
    sequence: 5,
    reasoningContent: '思考过程',
    toolCalls: [
      {
        id: 'call_abc',
        type: 'function',
        function: { name: 'search', arguments: '{"q":"test"}' },
      },
    ],
    timestamp: '2026-08-14T00:00:00Z',
    status: 'completed',
    thread_id: THREAD_ID,
    ...overrides,
  }
}

function snapshotMessage() {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg) return { found: false, content: '', status: '', parts: [] }
  return {
    found: true,
    content: msg.content || '',
    status: msg.status,
    parts: (msg.parts || []).map((p: any) => ({
      type: p.type,
      content: p.content || '',
      state: p.state,
      callId: p.callId,
      name: p.name,
      result: p.result,
      resultData: p.resultData,
    })),
  }
}

describe('new_message 完整消息形态（冷热同构）', () => {
  let usePipelineMessageStore: any
  let handleNewMessage: any
  let handleStreamStart: any

  beforeEach(async () => {
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
    handleNewMessage = handlerMod.handleNewMessage
    handleStreamStart = handlerMod.handleStreamStart
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  it('场景1：data.message 完整形态经共享 mapper 还原 thinking/tool_call/text parts', () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '最终回答文本',
      message: makeServerMessage(),
    }))

    const snap = snapshotMessage()
    expect(snap.found).toBe(true)
    expect(snap.status).toBe('completed')
    expect(snap.content).toBe('最终回答文本')

    const types = snap.parts.map((p: any) => p.type)
    // mapper 顺序：thinking → text → tool_call
    expect(types).toContain('thinking')
    expect(types).toContain('text')
    expect(types).toContain('tool_call')

    const thinkPart = snap.parts.find((p: any) => p.type === 'thinking')
    expect(thinkPart?.content).toContain('思考过程')
    expect(thinkPart?.state).toBe('done')

    const toolPart = snap.parts.find((p: any) => p.type === 'tool_call')
    expect(toolPart?.callId).toBe('call_abc')
    expect(toolPart?.name).toBe('search')
    expect(toolPart?.resultData ?? toolPart?.result).toBeUndefined() // 无结果事件 → 无 result
  })

  it('场景2：本地已累积的流式 parts 不被 data.message 覆盖（工具卡片保留）', () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    const handlerMod = (window as any).__handlers
    // 先流式累积工具卡片 + 正文（走真实 tool/stream handler 需重建，此处直接构造本地状态）
    const store = usePipelineMessageStore.getState()
    store.appendPart(PIPELINE_ID, MESSAGE_ID, {
      type: 'tool_call', callId: 'call_abc', name: 'search', args: { q: 'test' },
      state: 'calling',
    })
    store.appendPart(PIPELINE_ID, MESSAGE_ID, {
      type: 'text', content: '本地累积正文', state: 'streaming',
    })

    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '最终回答文本',
      message: makeServerMessage(),
    }))

    const snap = snapshotMessage()
    const toolParts = snap.parts.filter((p: any) => p.type === 'tool_call')
    expect(toolParts.length).toBe(1)
    expect(toolParts[0].callId).toBe('call_abc')
    // 本地正文保留（本地优先），不被 serverMessage 的 text 覆盖
    const textParts = snap.parts.filter((p: any) => p.type === 'text')
    const allText = textParts.map((p: any) => p.content).join('')
    expect(allText).toContain('本地累积正文')
    // 本地 streaming 态收敛为 done
    expect(snap.parts.every((p: any) => p.state !== 'streaming')).toBe(true)
  })

  it('场景3：流式中断（本地无内容）时 data.message 兜底填充完整形态', () => {
    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))

    // 无任何 chunk/tool 事件，直接 new_message（断线/丢包场景）
    handleNewMessage(makeEvent('new_message', {
      sequence: 5,
      content: '最终回答文本',
      message: makeServerMessage(),
    }))

    const snap = snapshotMessage()
    expect(snap.found).toBe(true)
    expect(snap.content).toBe('最终回答文本')
    const types = snap.parts.map((p: any) => p.type)
    expect(types).toContain('tool_call')
    expect(types).toContain('text')
    // thinking 也被还原（reasoningContent 经 mapper 生成）
    expect(types).toContain('thinking')
  })
})
