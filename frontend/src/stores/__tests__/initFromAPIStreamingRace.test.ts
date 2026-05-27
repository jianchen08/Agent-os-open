/**
 * 复现 initFromAPI 吃掉 streaming 消息的 bug
 *
 * 真实场景：用户发消息 → stream_start 创建占位符 → ChatContainer effect 触发 fetchMessages
 * → fetchMessages 返回 API 数据（不含 streaming 消息）→ initFromAPI 合并时吃掉 streaming 消息
 * → stream_end 到达时 updateMessage 找不到消息
 */
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

  it('场景A: initFromAPI 在 streaming 消息之后调用，streaming 消息必须保留', () => {
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
    const streamingMsg = afterInit.find(m => m.id === MESSAGE_ID)
    expect(streamingMsg).toBeDefined()
    expect(streamingMsg!.status).toBe('streaming')

    // stream_end 能找到
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)?.status).toBe('completed')
  })

  it('场景B: initFromAPI 在 streaming 消息之后调用，API 返回了不同 sequence 的消息', () => {
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

    // 关键检查：streaming 消息（WS 创建的 MESSAGE_ID）是否还在
    const wsMsg = msgs.find(m => m.id === MESSAGE_ID)
    const apiMsg = msgs.find(m => m.id === 'api-msg-same-seq')

    console.log('msgs after initFromAPI:', msgs.map(m => ({ id: m.id, seq: m.sequence, status: m.status })))

    // 至少有一个 sequence=2 的 assistant 消息存在
    const seq2Msgs = msgs.filter(m => m.sequence === 2 && m.role === 'assistant')
    expect(seq2Msgs.length).toBeGreaterThanOrEqual(1)

    // stream_end 用 MESSAGE_ID 能 updateMessage 吗？
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    const afterUpdate = store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)
    expect(afterUpdate).toBeDefined()
  })

  it('场景C: 用户消息通过 WS user_input 回来，和前端本地创建的重复', () => {
    const store = usePipelineMessageStore.getState()

    // 前端创建用户消息（handleSendMessage）
    store.addMessage(PIPELINE_ID, msg('client-user-1', 1, { role: 'user', content: 'hello' }))

    // 后端也推送了同一条用户消息（不同 ID，同 sequence）
    store.addMessage(PIPELINE_ID, msg('server-user-1', 1, { role: 'user', content: 'hello' }))

    // 应该只有 1 条（去重）
    const userMsgs = store.getMessages(PIPELINE_ID).filter(m => m.role === 'user')
    expect(userMsgs.length).toBe(1)

    // streaming 占位符
    store.startStreaming(PIPELINE_ID, MESSAGE_ID)
    store.addMessage(PIPELINE_ID, msg(MESSAGE_ID, 2, { status: 'streaming' }))

    // stream_end
    store.updateMessage(PIPELINE_ID, MESSAGE_ID, { status: 'completed' } as any)
    expect(store.getMessages(PIPELINE_ID).find(m => m.id === MESSAGE_ID)?.status).toBe('completed')
  })
})
