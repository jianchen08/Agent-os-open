import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

const logCalls: string[] = []

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: {
      debug: vi.fn(),
      info: vi.fn(),
      warn: vi.fn((...args: unknown[]) => { logCalls.push(args.filter(a => typeof a === 'string').join(' ')) }),
      error: vi.fn((...args: unknown[]) => { logCalls.push(args.filter(a => typeof a === 'string').join(' ')) }),
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
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
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
    logCalls.length = 0
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

    const historyMsgs: Message[] = []
    for (let i = 0; i < 10; i++) {
      historyMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}` }))
      historyMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, historyMsgs)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(20)

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

    const streamMsgId = 'msg_stream_001'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(22)

    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    store.finalizeMessage(PIPELINE_ID, streamMsgId)

    const finalMsgs = store.getMessages(PIPELINE_ID)
    const ended = finalMsgs.find(m => m.id === streamMsgId)
    expect(ended).toBeDefined()
    expect(ended!.status).toBe('completed')
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景2: initFromAPI 在 stream_start 之后被调用（竞态）', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    const historyMsgs: Message[] = []
    for (let i = 0; i < 5; i++) {
      historyMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}` }))
      historyMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, historyMsgs)

    const userSeq = store.getMessages(PIPELINE_ID).reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(PIPELINE_ID, makeMsg('user-new', { role: 'user', content: '新问题', sequence: userSeq }))

    const streamMsgId = 'msg_stream_002'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)).toBeDefined()

    _seq = 0
    const apiMsgs: Message[] = []
    for (let i = 0; i < 5; i++) {
      apiMsgs.push(makeMsg(`user-${i}`, { role: 'user', content: `问题${i}`, status: 'completed' }))
      apiMsgs.push(makeMsg(`assistant-${i}`, { role: 'assistant', content: `回答${i}`, status: 'completed' }))
    }
    apiMsgs.push(makeMsg('user-new', { role: 'user', content: '新问题', status: 'completed' }))
    store.initFromAPI(PIPELINE_ID, apiMsgs)

    const msgsAfterInit = store.getMessages(PIPELINE_ID)
    const streamingMsg = msgsAfterInit.find(m => m.id === streamMsgId)
    expect(streamingMsg).toBeDefined()

    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    const afterEnd = store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)
    expect(afterEnd).toBeDefined()
    expect(afterEnd!.status).toBe('completed')
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景3: stopStreaming 在 updateMessage 之前被调用（stream_end 顺序）', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    const streamMsgId = 'msg_stream_003'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    store.stopStreaming(PIPELINE_ID)

    const afterStop = store.getMessages(PIPELINE_ID)
    expect(afterStop).toHaveLength(1)
    expect(afterStop[0].status).toBe('completed')

    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    const afterUpdate = store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)
    expect(afterUpdate).toBeDefined()
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景4: pipelineId 不一致 - 消息写入 A 管道但 updateMessage 用 B 管道', () => {
    const store = usePipelineMessageStore.getState()
    const PIPELINE_A = 'pipeline_aaaaa'
    const PIPELINE_B = 'pipeline_bbbbb'

    store.registerPipeline({ pipelineId: PIPELINE_A, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.registerPipeline({ pipelineId: PIPELINE_B, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_A)

    store.addMessage(PIPELINE_A, makeMsg('user-1', { role: 'user', content: 'hello' }))

    const streamMsgId = 'msg_stream_004'
    ensureStreamingPlaceholder(store, PIPELINE_A, streamMsgId)

	    // updateMessage 在 B 管道找不到消息 → upsert 创建新消息（不会影响 A 管道）
	    store.updateMessage(PIPELINE_B, streamMsgId, { status: 'completed' } as any)

	    // upsert 日志
	    expect(logCalls.some(w => w.includes('updateMessage 未找到'))).toBe(true)

	    // A 管道的原消息不变
	    const msgsA = store.getMessages(PIPELINE_A)
	    expect(msgsA.find(m => m.id === streamMsgId)).toBeDefined()
	    expect(msgsA.find(m => m.id === streamMsgId)!.status).toBe('streaming')

	    // B 管道被 upsert 插入了新消息
	    const msgsB = store.getMessages(PIPELINE_B)
	    expect(msgsB.find(m => m.id === streamMsgId)).toBeDefined()
	    expect(msgsB.find(m => m.id === streamMsgId)!.status).toBe('completed')
  })

  it('场景5: stream_start 缺失导致占位消息未创建，updateMessage 自动创建消息', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    const historyMsgs: Message[] = []
    for (let i = 0; i < 25; i++) {
      historyMsgs.push(makeMsg(`hex_${i.toString(16).padStart(8, '0')}`, { role: 'user', content: `问题${i}` }))
      historyMsgs.push(makeMsg(`hex_${(i + 100).toString(16).padStart(8, '0')}`, { role: 'assistant', content: `回答${i}` }))
    }
    historyMsgs.push(makeMsg('user-c98dfca0-ec6', { role: 'user', content: '最新问题' }))
    store.initFromAPI(PIPELINE_ID, historyMsgs)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(51)

    const streamMsgId = 'msg_19743fdb'

    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(52)
    const autoCreated = msgs.find(m => m.id === streamMsgId)
    expect(autoCreated).toBeDefined()
    expect(autoCreated!.status).toBe('completed')
    expect(autoCreated!.role).toBe('assistant')
  })

  it('场景6: 真实 ID 格式竞态 — API hex ID vs WS msg_ 前缀 ID', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    const apiHistoryMsgs: Message[] = []
    for (let i = 1; i <= 25; i++) {
      apiHistoryMsgs.push(makeMsg(`hex_user_${i.toString().padStart(12, '0')}`, { role: 'user', content: `问题${i}` }))
      apiHistoryMsgs.push(makeMsg(`hex_asst_${i.toString().padStart(12, '0')}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, apiHistoryMsgs)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(50)

    const userSeq = store.getMessages(PIPELINE_ID).reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(PIPELINE_ID, makeMsg('c98dfca0-ec6b-4f5a', { role: 'user', content: '新问题', sequence: userSeq }))
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(51)

    const streamMsgId = 'msg_19743fdb'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)).toBeDefined()
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(52)

    _seq = 0
    const apiMsgs: Message[] = []
    for (let i = 1; i <= 25; i++) {
      apiMsgs.push(makeMsg(`hex_user_${i.toString().padStart(12, '0')}`, { role: 'user', content: `问题${i}`, status: 'completed' }))
      apiMsgs.push(makeMsg(`hex_asst_${i.toString().padStart(12, '0')}`, { role: 'assistant', content: `回答${i}`, status: 'completed' }))
    }
    apiMsgs.push(makeMsg('hex_user_new_0000', { role: 'user', content: '新问题', status: 'completed' }))
    store.initFromAPI(PIPELINE_ID, apiMsgs)

    const msgsAfterInit = store.getMessages(PIPELINE_ID)
    const streamingMsg = msgsAfterInit.find(m => m.id === streamMsgId)
    expect(streamingMsg).toBeDefined()
    expect(streamingMsg!.status).toBe('streaming')

    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    const afterEnd = store.getMessages(PIPELINE_ID).find(m => m.id === streamMsgId)
    expect(afterEnd).toBeDefined()
    expect(afterEnd!.status).toBe('completed')
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景7: stream_end 后 initFromAPI 覆盖已完成的占位消息', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    const apiHistoryMsgs: Message[] = []
    for (let i = 1; i <= 25; i++) {
      apiHistoryMsgs.push(makeMsg(`hex_user_${i.toString().padStart(12, '0')}`, { role: 'user', content: `问题${i}` }))
      apiHistoryMsgs.push(makeMsg(`hex_asst_${i.toString().padStart(12, '0')}`, { role: 'assistant', content: `回答${i}` }))
    }
    store.initFromAPI(PIPELINE_ID, apiHistoryMsgs)

    const userSeq = store.getMessages(PIPELINE_ID).reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.addMessage(PIPELINE_ID, makeMsg('c98dfca0-ec6b-4f5a', { role: 'user', content: '新问题', sequence: userSeq }))

    const streamMsgId = 'msg_19743fdb'
    ensureStreamingPlaceholder(store, PIPELINE_ID, streamMsgId)

    store.stopStreaming(PIPELINE_ID)
    store.updateMessage(PIPELINE_ID, streamMsgId, { status: 'completed' } as any)
    store.finalizeMessage(PIPELINE_ID, streamMsgId)

    _seq = 0
    const apiMsgs: Message[] = []
    for (let i = 1; i <= 25; i++) {
      apiMsgs.push(makeMsg(`hex_user_${i.toString().padStart(12, '0')}`, { role: 'user', content: `问题${i}`, status: 'completed' }))
      apiMsgs.push(makeMsg(`hex_asst_${i.toString().padStart(12, '0')}`, { role: 'assistant', content: `回答${i}`, status: 'completed' }))
    }
    apiMsgs.push(makeMsg('hex_user_new_0000', { role: 'user', content: '新问题', status: 'completed' }))
    store.initFromAPI(PIPELINE_ID, apiMsgs)

    const afterInit = store.getMessages(PIPELINE_ID)
    const completedMsg = afterInit.find(m => m.id === streamMsgId)
    expect(completedMsg).toBeDefined()
    expect(completedMsg!.status).toBe('completed')
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })

  it('场景8: addMessage sequence 去重 - 不同 role 同 sequence', () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({ pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null, agentName: '', status: 'idle', parentId: null, unreadCount: 0 })
    store.activatePipeline(PIPELINE_ID)

    store.addMessage(PIPELINE_ID, makeMsg('user-1', { role: 'user', content: 'hello', sequence: 1 }))

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

    const assistantMsg = msgs.find(m => m.id === 'msg_assistant_1')
    expect(assistantMsg).toBeDefined()

    store.updateMessage(PIPELINE_ID, 'msg_assistant_1', { status: 'completed' } as any)
    const afterUpdate = store.getMessages(PIPELINE_ID).find(m => m.id === 'msg_assistant_1')
    expect(afterUpdate).toBeDefined()
    expect(afterUpdate!.status).toBe('completed')
    expect(logCalls.some(w => w.includes('message not found'))).toBe(false)
  })
})
