import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
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
const MESSAGE_ID = 'msg_a37d345d'
const SESSION_ID = 'sess-test'

describe('initFromAPI 吃掉 streaming 消息', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  const msg = (id: string, seq: number, overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId: SESSION_ID,
    sequence: seq,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  })

  beforeEach(async () => {
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

  it('场景A: initFromAPI 在 streaming 消息之后调用，本地 streaming 消息被丢弃（刷新=全量重载）', () => {
    const store = usePipelineMessageStore.getState()

    // 初始加载
    store.initFromAPI(PIPELINE_ID, [
      msg('user-1', 1, { role: 'user', content: 'hello' }),
    ])

    // 用户发消息（前端本地创建）
    store.addMessage(PIPELINE_ID, msg('user-2', 2, { role: 'user', content: 'world' }))

    // stream_start: 创建 streaming 占位符
    const seq = store.getMessages(PIPELINE_ID).reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1
    store.startStreaming(PIPELINE_ID, MESSAGE_ID)
    store.addMessage(PIPELINE_ID, msg(MESSAGE_ID, seq, { status: 'streaming' }))

    expect(store.getMessages(PIPELINE_ID)).toHaveLength(3)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)).toBeDefined()

    // ⚠️ 关键操作：initFromAPI 再次调用（API 还没有 streaming 消息）
    store.initFromAPI(PIPELINE_ID, [
      msg('user-1', 1, { role: 'user', content: 'hello' }),
      msg('user-2', 2, { role: 'user', content: 'world' }),
    ])

    const afterInit = store.getMessages(PIPELINE_ID)
    // 新语义：initFromAPI 完全丢弃本地消息（含 streaming 占位符），只用 API 权威数据
    expect(afterInit.find(m => m.id === MESSAGE_ID)).toBeUndefined()
    expect(afterInit).toHaveLength(2)
    expect(afterInit.map(m => m.id)).toEqual(['user-1', 'user-2'])

    // 被丢弃的 streaming 占位符不会被 updateMessage 重新创建
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)).toBeUndefined()
  })

  it('场景B: initFromAPI 在 streaming 消息之后调用，本地 streaming 占位符被丢弃，仅保留 API 返回的消息', () => {
    const store = usePipelineMessageStore.getState()

    // 初始加载
    store.initFromAPI(PIPELINE_ID, [
      msg('user-1', 1, { role: 'user', content: 'hello' }),
    ])

    // streaming 占位符
    store.startStreaming(PIPELINE_ID, MESSAGE_ID)
    store.addMessage(PIPELINE_ID, msg(MESSAGE_ID, 2, { status: 'streaming' }))

    // initFromAPI: API 返回了 sequence=2 的 assistant 消息（不同 ID）
    store.initFromAPI(PIPELINE_ID, [
      msg('user-1', 1, { role: 'user', content: 'hello' }),
      msg('api-msg-same-seq', 2, { content: 'full response from api', status: 'completed' }),
    ])

    const msgs = store.getMessages(PIPELINE_ID)

    // 新语义：本地 streaming 占位符（MESSAGE_ID）被丢弃，只剩 API 权威数据
    expect(msgs.find(m => m.id === MESSAGE_ID)).toBeUndefined()
    const apiMsg = msgs.find(m => m.id === 'api-msg-same-seq')
    expect(apiMsg).toBeDefined()
    expect(apiMsg!.content).toBe('full response from api')
    expect(msgs).toHaveLength(2)

    // sequence=2 的 assistant 消息只有 API 那一条（不再保留 WS 占位符）
    const seq2Msgs = msgs.filter(m => m.sequence === 2 && m.role === 'assistant')
    expect(seq2Msgs.length).toBe(1)
    expect(seq2Msgs[0].id).toBe('api-msg-same-seq')

    // 被丢弃的 streaming 占位符不会被 updateMessage 重新创建
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)).toBeUndefined()
  })

  it('场景C: 乐观 user 消息通过 clientMessageId 对账去重', () => {
    const store = usePipelineMessageStore.getState()

    // 前端创建乐观用户消息（带 clientMessageId）
    store.addMessage(PIPELINE_ID, msg('client-user-1', 1, { role: 'user', content: 'hello', clientMessageId: 'client-user-1' }))

    // 后端 initFromAPI 返回同一条消息（不同后端 id，相同 clientMessageId）
    store.initFromAPI(PIPELINE_ID, [
      msg('server-user-1', 1, { role: 'user', content: 'hello', clientMessageId: 'client-user-1' }),
    ])

    // 对账后应只有 1 条（后端权威版本替换乐观版本）
    const userMsgs = store.getMessages(PIPELINE_ID).filter(m => m.role === 'user')
    expect(userMsgs.length).toBe(1)
    expect(userMsgs[0].id).toBe('server-user-1')

    // streaming 占位符
    store.startStreaming(PIPELINE_ID, MESSAGE_ID)
    store.addMessage(PIPELINE_ID, msg(MESSAGE_ID, 2, { status: 'streaming' }))

    // stream_end
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)?.status).toBe('completed')
  })
})
