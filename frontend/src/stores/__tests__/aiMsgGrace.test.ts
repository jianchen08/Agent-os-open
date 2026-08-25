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

  it('场景1: 刚完成的 AI 回复（新鲜 _lastUpdated）在迟到 init 后保留——快照未含不抹掉', () => {
    const store = usePipelineMessageStore.getState()

    // 本页刚收尾的 AI 回复（stream_end/new_message 收尾：status=completed + 新鲜 _lastUpdated 戳）
    const freshAiMsg = makeMsg('a_38c5e8cbe88f49fda3cd1974c7347c83', 2, {
      role: 'assistant',
      content: 'hello, this is the AI reply',
      status: 'completed',
      _lastUpdated: Date.now(),
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, freshAiMsg)
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(1)

    // initFromAPI 不含该消息（快照查询发生在落库前——迟到响应竞态）
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-1', 1, { role: 'user', content: 'question' }),
    ])

    // 契约：快照发起早于本页收尾活动时，刚出现的回复不得被全量替换抹掉；
    // 后续更晚的快照会带同 id 记录，isCoveredByApi 让位权威版（不并存）。
    const kept = store.getMessages(PIPELINE_ID).find((m) => m.id === 'a_38c5e8cbe88f49fda3cd1974c7347c83')
    expect(kept?.content).toBe('hello, this is the AI reply')
    expect(store.getMessages(PIPELINE_ID).filter((m) => m.role === 'assistant')).toHaveLength(1)
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

  it('场景3: AI 消息落库后 API 返回同 id 记录 → 让位权威版，只留一条（不重复渲染）', () => {
    const store = usePipelineMessageStore.getState()

    // 流式占位 id = 后端落库 record_id（a_ id 契约：stream_start 与落库同 id）
    const localAiMsg = makeMsg('a_ws_same_id_3', 5, {
      role: 'assistant',
      content: 'AI reply via WS',
      status: 'completed',
      _lastUpdated: Date.now(),
    })
    store.addMessage(PIPELINE_ID, localAiMsg)

    // API 返回同 id 的权威记录
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-3', 4, { role: 'user', content: 'q' }),
      makeMsg('a_ws_same_id_3', 5, {
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

  it('场景4: 无 _lastUpdated 戳的 streaming 消息不享受保留（真实占位必打戳，无戳=残影）', () => {
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

    // 契约：保留判定以 _lastUpdated 戳为准（ensureStreamingPlaceholder 必打戳）；
    // 无戳的 streaming 视为残影丢弃，避免刷新后幽灵气泡复活。
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

  it('场景6: 乐观 user（cmid 新鲜）在迟到 init 后保留——快照未含不抹掉', () => {
    const store = usePipelineMessageStore.getState()

    // 乐观 user：发送瞬间直写主数组（单一消息数组协议，无独立 pending 区）
    const optimisticUser = makeMsg('client-uuid-6', 1, {
      role: 'user',
      content: 'hello',
      status: 'sending',
      clientMessageId: 'client-uuid-6',
      timestamp: new Date().toISOString(),
    })
    store.addMessage(PIPELINE_ID, optimisticUser)

    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-ai-6', 2, { role: 'assistant', content: 'reply' }),
    ])

    // 契约：快照发起早于发送时，乐观 user 保留（「发送后用户消息消失」回归锚）；
    // 落库后的快照会带同 cmid 记录，isCoveredByApi 让位权威版（不并存）。
    const userMsg = store.getMessages(PIPELINE_ID).find((m) => m.clientMessageId === 'client-uuid-6')
    expect(userMsg).toBeDefined()
    expect(userMsg?.content).toBe('hello')
  })

  // ★ 回归保护：同一逻辑消息的本地气泡与 API 记录必须按 id 收敛为一条，
  // 保留机制不得造成双气泡重复渲染。
  it('场景7: 本地 assistant 与 API 记录同 id → 让位 API 权威版，只留一条', () => {
    const store = usePipelineMessageStore.getState()

    // 本页流式收尾的气泡（id 即后端 record_id）
    const localMsg = makeMsg('a_same_id_777', 2, {
      role: 'assistant',
      content: 'AI 回复',
      status: 'completed',
      _lastUpdated: Date.now(),
    })
    store.addMessage(PIPELINE_ID, localMsg)

    // API 返回同 id 的权威记录
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('api-user-7', 1, { role: 'user', content: '问' }),
      makeMsg('a_same_id_777', 3, {
        role: 'assistant',
        content: 'AI 回复',
        status: 'completed',
      }),
    ])

    // ★ 核心断言：同 id 收敛——只剩 API 权威版 1 条，不重复渲染
    const aiMsgs = store.getMessages(PIPELINE_ID).filter((m) => m.role === 'assistant')
    expect(aiMsgs).toHaveLength(1)
    expect(aiMsgs[0].id).toBe('a_same_id_777')
  })
})
