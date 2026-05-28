/**
 * 复现 "发送消息没有消息输出，但刷新后有" 的 bug
 *
 * 模拟多轮对话场景：历史消息很多 → 用户发消息 → stream_start/chunk/end
 * 验证 updateMessage 在各种场景下能否找到消息
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

const warnCalls: string[] = []

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn((...args: unknown[]) => { warnCalls.push(args.filter(a => typeof a === 'string').join(' ')) }),
      error: vi.fn(),
    },
    websocket: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn(),
      error: vi.fn(),
    },
  },
}))

vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = '39ef1314a7b9'
const SESSION_ID = 'sess-test-1'

describe('发送消息没有输出 bug 复现', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  let _seq = 0
  const nextSeq = () => ++_seq

  const makeMsg = (id: string, overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId: SESSION_ID,
    sequence: nextSeq(),
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + _seq * 100).toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  })

  const ensureStreamingPlaceholder = (
    store: any,
    pipelineId: string,
    messageId: string,
  ) => {
    store.startStreaming(pipelineId, messageId)
    const existingMsgs = store.getMessages(pipelineId)
    const seq = existingMsgs.reduce((max: number, m: any) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(pipelineId, {
      id: messageId,
      sessionId: SESSION_ID,
      role: 'assistant',
      content: '',
      sequence: seq,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'streaming',
    } as Message)
  }

  beforeEach(async () => {
    _seq = 0
    warnCalls.length = 0
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
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
  })

  it('场景1: 多轮历史后发消息 - initFromAPI 先加载大量历史', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    // 1. 模拟 API 加载大量历史消息（10轮对话）
    const historyMsgs: Message[] = []
    for (let i = 0; i < 10; i++) {
      historyMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}` }))
      historyMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, historyMsgs)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(20)

    // 2. 用户发新消息（前端乐观写入）
    const userMsgId = 'user-new-1'
    const existingMsgs = store.getMessages(PIPELINE_ID)
    const userSeq = existingMsgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(PIPELINE_ID, {
      id: userMsgId,
      sessionId: SESSION_ID,
      role: 'user',
      content: '新问题',
      sequence: userSeq,
      timestamp: new Date().toISOString(),
      parentId: null,
    } as Message)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(21)

    // 3. 后端 stream_start → ensureStreamingPlaceholder
    const streamMsgId = 'msg_stream_001'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(22)

    // 4. stream_end → updateMessage
    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    store.finalizeMessage(PIPELINE_ID, streamMsgId)

    const finalMsgs = store.getMessages(PIPELINE_ID)
    const ended = finalMsgs.find(m => m.id === streamMsgId)
    expect(ended).toBeDefined()
    expect(ended!.status).toBe('completed')
    expect(warnCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景2: initFromAPI 在 stream_start 之后被调用（竞态）', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    // 1. 先加载历史
    const historyMsgs: Message[] = []
    for (let i = 0; i < 5; i++) {
      historyMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}` }))
      historyMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, historyMsgs)

    // 2. 用户发消息
    const userSeq = store.getMessages(PIPELINE_ID).reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(PIPELINE_ID, makeMsg('user-new', { role: 'user', content: '新问题', sequence: userSeq }))

    // 3. stream_start 创建占位符
    const streamMsgId = 'msg_stream_002'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)).toBeDefined()

    // 4. ★ 竞态：fetchMessages 返回（比如 setSessionActive 触发），API 没有 streaming 消息
    //    initFromAPI 用 API 数据覆盖，但 API 的消息列表还没有 streaming 消息
    _seq = 0
    const apiMsgs: Message[] = []
    for (let i = 0; i < 5; i++) {
      apiMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}`, status: 'completed' }))
      apiMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}`, status: 'completed' }))
    }
    apiMsgs.push(makeMsg('user-new', { role: 'user', content: '新问题', status: 'completed' }))
    store.initFromAPI(PIPELINE_ID, apiMsgs)

    // 5. streaming 消息应该被保留
    const msgsAfterInit = store.getMessages(PIPELINE_ID)
    const streamingMsg = msgsAfterInit.find(m => m.id === streamMsgId)
    expect(streamingMsg).toBeDefined()

    // 6. stream_end 应该能找到消息
    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    const afterEnd = store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)
    expect(afterEnd).toBeDefined()
    expect(afterEnd!.status).toBe('completed')
    expect(warnCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景3: stopStreaming 在 updateMessage 之前被调用（stream_end 顺序）', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    // 创建占位符
    const streamMsgId = 'msg_stream_003'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // 模拟 handleStreamEnd 的实际调用顺序：
    // 1. terminatePipeline → stopStreaming（会将消息标记为 completed）
    // 2. updateMessage(pipelineId, messageId, { status: 'completed' })

    store.stopStreaming(PIPELINE_ID)

    // stopStreaming 后消息应该还在
    const afterStop = store.getMessages(PIPELINE_ID)
    expect(afterStop).toHaveLength(1)
    expect(afterStop[0].status).toBe('completed')

    // updateMessage 应该能找到（因为 stopStreaming 已经标记为 completed）
    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    const afterUpdate = store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)
    expect(afterUpdate).toBeDefined()
    expect(warnCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景4: pipelineId 不一致 - 消息写入 A 管道但 updateMessage 用 B 管道', () => {
    const store = usePipelineMessageStore.getState()
    const PIPELINE_A = 'pipeline_aaaaa'
    const PIPELINE_B = 'pipeline_bbbbb'

    store.registerPipeline({ pipelineId: PIPELINE_A, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.registerPipeline({ pipelineId: PIPELINE_B, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_A)

    // 用户发消息写入 PIPELINE_A
    store.addMessage(PIPELINE_A, makeMsg('user-1', { role: 'user', content: 'hello' }))

    // stream_start 创建占位符写入 PIPELINE_A
    const streamMsgId = 'msg_stream_004'
    ensureStreamingPlaceholder(store, PIPELINE_A, streamMsgId)

    // ★ 模拟 BUG: updateMessage 用了 PIPELINE_B（pipelineId 不匹配）
    store.updateMessage(PIPELINE_B, streamMsgId, { status: 'completed' } as any)

    // 应该产生 WARN 日志
    expect(warnCalls.some(w => w.includes('message not found'))).toBe(true)

    // PIPELINE_A 中的消息还在
    const msgsA = store.getMessages(PIPELINE_A)
    expect(msgsA.find(m => m.id === streamMsgId)).toBeDefined()
    expect(msgsA.find(m => m.id === streamMsgId)!.status).toBe('streaming')
  })

  it('场景5: addMessage sequence 去重 - 不同 role 同 sequence', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    // 用户消息 seq=1
    store.addMessage(PIPELINE_ID, makeMsg('user-1', { role: 'user', content: 'hello', sequence: 1 }))

    // assistant 占位符也用 seq=1（如果 ensureStreamingPlaceholder 计算有误）
    store.addMessage(PIPELINE_ID, {
      id: 'msg_assistant_1',
      sessionId: SESSION_ID,
      role: 'assistant',
      content: '',
      sequence: 1,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'streaming',
    } as Message)

    const msgs = store.getMessages(PIPELINE_ID)

    // 关键断言：assistant 消息应该存在且 ID 为 msg_assistant_1
    const assistantMsg = msgs.find(m => m.id === 'msg_assistant_1')
    expect(assistantMsg).toBeDefined()

    // updateMessage 用 assistant ID 必须能找到
    store.updateMessage(PIPELINE_ID, 'msg_assistant_1', { status: 'completed' } as any)
    const afterUpdate = store.getMessages(PIPELINE_ID).find(m => m.id === 'msg_assistant_1')
    expect(afterUpdate).toBeDefined()
    expect(afterUpdate!.status).toBe('completed')
    expect(warnCalls.some(w => w.includes('message not found'))).toBe(false)
  })
})
