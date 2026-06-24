/**
 * AI 消息宽限期保留测试
 *
 * BUG-FIX-fix_20260624_ai_msg_vanish:
 * 验证刚 finalize 完成的 AI 消息（stream_end 后 status 变 'completed'）在后端
 * 尚未持久化时（initFromAPI 未返回）不会被丢弃。与乐观 user 消息同源问题。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { usePipelineMessageStore } from '../pipelineMessageStore'
import type { Message } from '@/types/models'

const PIPELINE_ID = 'test-pipeline-aigrace'
const SESSION_ID = 'test-session-aigrace'

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

describe('AI 消息宽限期', () => {
  beforeEach(() => {
    const store = usePipelineMessageStore.getState()
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

  it('场景1: 刚 finalize 的 AI 消息，API 未返回时不被丢弃', () => {
    const store = usePipelineMessageStore.getState()

    // AI 回复刚 stream_end 完成：status='completed'，_lastUpdated 为当前时间
    // （finalizeMessage 会写入 _lastUpdated: Date.now()，这里模拟该状态）
    const freshAiMsg = makeMsg('ai-uuid-1', 2, {
      role: 'assistant',
      content: 'hello, this is the AI reply',
      status: 'completed',
      _lastUpdated: Date.now(), // 刚 finalize，在宽限期内
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, freshAiMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 被并发触发（WS 重连/Tab 切换），后端尚未持久化该 AI 消息，
    // API 只返回 user 消息
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-1', 1, { role: 'user', content: 'question' }),
    ])

    // 刚完成的 AI 消息应仍然存在（未被丢弃）
    const msgs = store.getMessages(PIPELINE_ID)
    const aiMsg = msgs.find((m) => m.id === 'ai-uuid-1')
    expect(aiMsg).toBeDefined()
    expect(aiMsg?.content).toBe('hello, this is the AI reply')
  })

  it('场景2: AI 消息在宽限期外的 persist 残留被丢弃', () => {
    const store = usePipelineMessageStore.getState()

    // persist 残留的旧 AI 消息（_lastUpdated 为 2 分钟前，远超 30s 宽限期）
    const staleAiMsg = makeMsg('stale-ai-2', 2, {
      role: 'assistant',
      content: 'stale AI reply',
      status: 'completed',
      _lastUpdated: Date.now() - 120_000, // 2 分钟前
      timestamp: new Date(Date.now() - 120_000).toISOString(),
    })
    store.addMessage(PIPELINE_ID, staleAiMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 返回新数据，不含此旧消息
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-2', 1, { role: 'user', content: 'new question' }),
    ])

    // 旧 AI 消息应被丢弃（不在宽限期内）
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'stale-ai-2')).toBeUndefined()
  })

  it('场景3: AI 消息被后端持久化后通过 role::seq 指纹去重（不重复渲染）', () => {
    const store = usePipelineMessageStore.getState()

    // AI 消息刚 finalize（本地 WS UUID）
    const localAiMsg = makeMsg('ws-uuid-3', 5, {
      role: 'assistant',
      content: 'AI reply via WS',
      status: 'completed',
      _lastUpdated: Date.now(),
    })
    store.addMessage(PIPELINE_ID, localAiMsg)

    // 后端持久化后，API 返回该消息（不同 id=API hex，相同 sequence=5）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-3', 4, { role: 'user', content: 'q' }),
      makeMsg('api-hex-3', 5, {
        role: 'assistant',
        content: 'AI reply via WS', // 同 sequence 同 role，指纹相同
      }),
    ])

    // 应只有 1 条 sequence=5 的 assistant 消息（去重后不并存，避免气泡重复渲染）
    const aiMsgs = store
      .getMessages(PIPELINE_ID)
      .filter((m) => m.role === 'assistant' && m.sequence === 5)
    expect(aiMsgs).toHaveLength(1)
    expect(aiMsgs[0].content).toBe('AI reply via WS')
  })

  it('场景4: streaming 中的 AI 消息始终保留（不受宽限期影响）', () => {
    const store = usePipelineMessageStore.getState()

    // 正在流式的 AI 占位消息
    const streamingMsg = makeMsg('streaming-4', 2, {
      role: 'assistant',
      content: '',
      status: 'streaming',
    })
    store.startStreaming(PIPELINE_ID, 'streaming-4')
    store.addMessage(PIPELINE_ID, streamingMsg)

    // initFromAPI 不含该消息（后端尚未持久化）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-4', 1, { role: 'user', content: 'hi' }),
    ])

    // streaming 占位消息应保留
    const streaming = store.getMessages(PIPELINE_ID).find((m) => m.id === 'streaming-4')
    expect(streaming).toBeDefined()
    expect(streaming?.status).toBe('streaming')
  })

  it('场景5: 无 _lastUpdated 的 assistant 消息不享受宽限期（直接丢弃）', () => {
    const store = usePipelineMessageStore.getState()

    // addMessage 直接创建的 assistant 消息不会写 _lastUpdated。
    // 此类消息若无后续 finalize/updateMessage，说明不是刚完成的流式回复，
    // 不应享受乐观窗口（避免误保留脏数据）。
    const noLastUpdated = makeMsg('no-lu-5', 3, {
      role: 'assistant',
      content: 'assistant msg without _lastUpdated',
      status: 'completed',
    })
    delete (noLastUpdated as Partial<Message>)._lastUpdated
    store.addMessage(PIPELINE_ID, noLastUpdated)

    // initFromAPI 未返回该消息
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-5', 1, { role: 'user', content: 'q' }),
    ])

    // 无 _lastUpdated 的 assistant 消息应被丢弃（无判据，不享受宽限期）
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'no-lu-5')).toBeUndefined()
  })

  it('场景6: 修复未破坏既有 user 消息宽限期行为', () => {
    const store = usePipelineMessageStore.getState()

    // 乐观 user 消息（带 clientMessageId）
    const optimisticUser = makeMsg('client-uuid-6', 1, {
      role: 'user',
      content: 'hello',
      status: 'completed',
      clientMessageId: 'client-uuid-6',
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, optimisticUser)

    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-ai-6', 2, { role: 'assistant', content: 'reply' }),
    ])

    // user 消息仍应保留（回归保护）
    const userMsg = store.getMessages(PIPELINE_ID).find((m) => m.role === 'user')
    expect(userMsg).toBeDefined()
    expect(userMsg?.content).toBe('hello')
  })
})
