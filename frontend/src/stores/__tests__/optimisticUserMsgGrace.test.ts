/**
 * 乐观 user 消息宽限期保留测试
 *
 * BUG-FIX-fix_20260623_optimistic_user_msg_vanish:
 * 验证刚发送的乐观 user 消息在后端尚未持久化时（initFromAPI 未返回）不会被丢弃。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { usePipelineMessageStore } from '../pipelineMessageStore'
import type { Message } from '@/types/models'

const PIPELINE_ID = 'test-pipeline-opt'
const SESSION_ID = 'test-session-opt'

function makeMsg(
  id: string,
  sequence: number | null,
  overrides: Partial<Message> = {},
): Message {
  return {
    id,
    sessionId: SESSION_ID,
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    sequence: sequence ?? undefined,
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('乐观 user 消息宽限期', () => {
  beforeEach(() => {
    const store = usePipelineMessageStore.getState()
    // 清理该 pipeline 的数据
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        ...store.messagesByPipeline,
        [PIPELINE_ID]: [],
      },
    })
    store.registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: SESSION_ID,
      level: 1,
      tabId: null,
      agentName: '',
      status: 'idle',
      parentId: null,
      unreadCount: 0,
    })
  })

  it('场景1: 刚发送的乐观 user 消息，API 未返回时不被丢弃', () => {
    const store = usePipelineMessageStore.getState()

    // 用户发送乐观消息（带 clientMessageId，timestamp 为当前时间）
    const optimisticMsg = makeMsg('client-uuid-1', 1, {
      role: 'user',
      content: 'hello world',
      status: 'completed',
      clientMessageId: 'client-uuid-1',
      timestamp: new Date().toISOString(), // 当前时间，在宽限期内
    })
    store.addMessage(PIPELINE_ID, optimisticMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 被触发（如 WS 重连），后端尚未持久化 user 消息，API 只返回旧历史
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-old-1', 1, { role: 'assistant', content: 'old reply' }),
    ])

    // 乐观消息应仍然存在（未被丢弃）
    const msgs = store.getMessages(PIPELINE_ID)
    const userMsg = msgs.find((m) => m.role === 'user')
    expect(userMsg).toBeDefined()
    expect(userMsg?.content).toBe('hello world')
  })

  it('场景2: 乐观 user 消息在宽限期外的 persist 残留被丢弃', () => {
    const store = usePipelineMessageStore.getState()

    // persist 残留的旧消息（timestamp 为 2 分钟前，远超 30s 宽限期）
    const staleMsg = makeMsg('stale-uuid-2', 1, {
      role: 'user',
      content: 'stale msg',
      status: 'completed',
      clientMessageId: 'stale-uuid-2',
      timestamp: new Date(Date.now() - 120_000).toISOString(), // 2分钟前
    })
    store.addMessage(PIPELINE_ID, staleMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 返回 API 数据，不含此旧消息
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-new-1', 1, { role: 'assistant', content: 'api reply' }),
    ])

    // 旧消息应被丢弃（不在宽限期内）
    const msgs = store.getMessages(PIPELINE_ID)
    const staleUserMsg = msgs.find((m) => m.id === 'stale-uuid-2')
    expect(staleUserMsg).toBeUndefined()
  })

  it('场景3: 乐观 user 消息被后端持久化后通过 clientMessageId 对账替换', () => {
    const store = usePipelineMessageStore.getState()

    // 用户发送乐观消息
    const optimisticMsg = makeMsg('client-uuid-3', 1, {
      role: 'user',
      content: 'hello',
      status: 'completed',
      clientMessageId: 'client-uuid-3',
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, optimisticMsg)

    // 后端持久化后，API 返回该消息（不同 id，相同 clientMessageId）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('server-uuid-3', 1, {
        role: 'user',
        content: 'hello',
        clientMessageId: 'client-uuid-3',
      }),
      makeMsg('api-asst-3', 2, { role: 'assistant', content: 'reply' }),
    ])

    // 应只有 1 条 user 消息（后端权威版本替换乐观版本）
    const userMsgs = store.getMessages(PIPELINE_ID).filter((m) => m.role === 'user')
    expect(userMsgs).toHaveLength(1)
    expect(userMsgs[0].id).toBe('server-uuid-3')
  })

  it('场景4: 无 clientMessageId 的本地消息不享受宽限期（直接丢弃）', () => {
    const store = usePipelineMessageStore.getState()

    // 本地消息无 clientMessageId（如 persist 恢复的旧 assistant 消息）
    const noClientMsg = makeMsg('no-client-4', 1, {
      role: 'assistant',
      content: 'old assistant',
      status: 'completed',
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, noClientMsg)

    // initFromAPI 返回不含此消息
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-4', 1, { role: 'user', content: 'question' }),
    ])

    // 无 clientMessageId 的消息应被丢弃
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'no-client-4')).toBeUndefined()
  })

  it('场景5: streaming 占位消息仍然始终保留', () => {
    const store = usePipelineMessageStore.getState()

    // 创建 streaming 占位消息
    const streamingMsg = makeMsg('stream-5', 2, {
      role: 'assistant',
      content: '',
      status: 'streaming',
    })
    store.startStreaming(PIPELINE_ID, 'stream-5')
    store.addMessage(PIPELINE_ID, streamingMsg)

    // initFromAPI 不含 streaming 消息（后端尚未持久化）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-5', 1, { role: 'user', content: 'hi' }),
    ])

    // streaming 占位消息应保留
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'stream-5')).toBeDefined()
    expect(msgs.find((m) => m.id === 'stream-5')?.status).toBe('streaming')
  })
})
