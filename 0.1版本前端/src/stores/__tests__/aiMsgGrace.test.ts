/** AI 消息刷新对账测试（assistant 不走乐观宽限期） */
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

describe('AI 消息刷新对账（initFromAPI 全量权威）', () => {
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

  it('场景1: 已完成的 AI 消息，API 未返回时信任 API 丢弃（全量对账权威）', () => {
    const store = usePipelineMessageStore.getState()

    // 已完成 AI 消息：非 streaming，API 不含它 → initFromAPI 全量对账应丢弃
    const freshAiMsg = makeMsg('ai-uuid-1', 2, {
      role: 'assistant',
      content: 'hello, this is the AI reply',
      status: 'completed',
      _lastUpdated: Date.now(),
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, freshAiMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 不含该消息（后端尚未持久化）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-1', 1, { role: 'user', content: 'question' }),
    ])

    // assistant 不走乐观宽限期：全量对账信任 API 权威，本地消息被丢弃。
    // 后端持久化后，下一次增量 API 调用（append/prepend）会拉回该消息。
    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'ai-uuid-1')).toBeUndefined()
  })

  it('场景2: AI 消息在宽限期外的 persist 残留被丢弃（不变）', () => {
    const store = usePipelineMessageStore.getState()

    const staleAiMsg = makeMsg('stale-ai-2', 2, {
      role: 'assistant',
      content: 'stale AI reply',
      status: 'completed',
      _lastUpdated: Date.now() - 120_000,
      timestamp: new Date(Date.now() - 120_000).toISOString(),
    })
    store.addMessage(PIPELINE_ID, staleAiMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-2', 1, { role: 'user', content: 'new question' }),
    ])

    expect(store.getMessages(PIPELINE_ID).find((m) => m.id === 'stale-ai-2')).toBeUndefined()
  })

  it('场景3: AI 消息被后端持久化后通过 role::seq 指纹去重（不重复渲染）', () => {
    const store = usePipelineMessageStore.getState()

    const localAiMsg = makeMsg('ws-uuid-3', 5, {
      role: 'assistant',
      content: 'AI reply via WS',
      status: 'completed',
      _lastUpdated: Date.now(),
    })
    store.addMessage(PIPELINE_ID, localAiMsg)

    // API 返回同 seq 的记录 → 指纹匹配，去重
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-3', 4, { role: 'user', content: 'q' }),
      makeMsg('api-hex-3', 5, {
        role: 'assistant',
        content: 'AI reply via WS',
      }),
    ])

    // 只剩 1 条 sequence=5 的 assistant（API 权威版覆盖本地版）
    const aiMsgs = store
      .getMessages(PIPELINE_ID)
      .filter((m) => m.role === 'assistant' && m.sequence === 5)
    expect(aiMsgs).toHaveLength(1)
    expect(aiMsgs[0].content).toBe('AI reply via WS')
  })

  it('场景4: streaming 中的 AI 消息也被 initFromAPI 丢弃（靠 WS backfill 恢复）', () => {
    const store = usePipelineMessageStore.getState()

    const streamingMsg = makeMsg('streaming-4', 2, {
      role: 'assistant',
      content: '',
      status: 'streaming',
    })
    store.startStreaming(PIPELINE_ID, 'streaming-4')
    store.addMessage(PIPELINE_ID, streamingMsg)

    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-4', 1, { role: 'user', content: 'hi' }),
    ])

    // 新语义：initFromAPI 完全丢弃本地（含 streaming），只保留 API 权威数据。
    // 正在输出的内容不靠刷新兜底，而是由 WS 重连的 backfill（appendMessages）
    // + 续流补回，避免刷新瞬间用陈旧本地缓存与后端权威数据并存造成重复渲染。
    const streaming = store.getMessages(PIPELINE_ID).find((m) => m.id === 'streaming-4')
    expect(streaming).toBeUndefined()
  })

  it('场景5: 无 _lastUpdated 的 assistant 消息不享受宽限期（直接丢弃）', () => {
    const store = usePipelineMessageStore.getState()

    const noLastUpdated = makeMsg('no-lu-5', 3, {
      role: 'assistant',
      content: 'assistant msg without _lastUpdated',
      status: 'completed',
    })
    delete (noLastUpdated as Partial<Message>)._lastUpdated
    store.addMessage(PIPELINE_ID, noLastUpdated)

    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-5', 1, { role: 'user', content: 'q' }),
    ])

    expect(store.getMessages(PIPELINE_ID).find((m) => m.id === 'no-lu-5')).toBeUndefined()
  })

  it('场景6: 新语义下 user 乐观消息也被 initFromAPI 丢弃（API 不含即不保留）', () => {
    const store = usePipelineMessageStore.getState()

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

    // 新语义：initFromAPI 不再保留任何 localOnly（含 user 乐观宽限期）。
    // API 未返回该 user 消息（后端尚未持久化）时，刷新后该消息被丢弃；
    // 后端持久化后，下一次 API 调用会带它回来。
    const userMsg = store.getMessages(PIPELINE_ID).find((m) => m.role === 'user')
    expect(userMsg).toBeUndefined()
  })

  // ★ 回归保护：ensureStreamingPlaceholder 合并覆盖 id 后，
  // 本地气泡 id ≠ API record_id → 不应通过宽限期保留，防止 AI 回复重复渲染。
  it('场景7: 本地 assistant id 与 API record_id 不同时，不走宽限期保留，只留 API 版', () => {
    const store = usePipelineMessageStore.getState()

    // 模拟 ensureStreamingPlaceholder 合并：本地气泡 id 是合并后的 id（hex_222...），
    // 但 API record_id 是第一次 emit_start 的 id（hex_111...）
    const mergedLocalMsg = makeMsg('hex_222222222222', 2, {
      role: 'assistant',
      content: '合并后的 AI 回复',
      status: 'completed',
      _lastUpdated: Date.now(),
    })
    store.addMessage(PIPELINE_ID, mergedLocalMsg)

    // API 返回同 content 但不同 id 的记录（第一次 emit_start 的 record_id）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-7', 1, { role: 'user', content: '问' }),
      makeMsg('hex_111111111111', 3, {
        role: 'assistant',
        content: '合并后的 AI 回复',
        status: 'completed',
      }),
    ])

    // ★ 核心断言：assistant 不走宽限期 → 本地 id 不同的消息被丢弃，
    // 只剩 API 版 1 条，不重复渲染
    const aiMsgs = store.getMessages(PIPELINE_ID).filter((m) => m.role === 'assistant')
    expect(aiMsgs).toHaveLength(1)
    expect(aiMsgs[0].id).toBe('hex_111111111111')
  })
})
