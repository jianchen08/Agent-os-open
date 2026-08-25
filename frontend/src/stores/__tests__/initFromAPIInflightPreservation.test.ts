/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * initFromAPI 飞行中消息保留契约。
 *
 * 契约：initFromAPI 的刷新语义是「API 权威替换」，但快照发起早于本页消息活动时
 * （刷新后后台对账 fetch 与发送/流式收尾竞态——快照查询在消息落库前执行、响应
 * 在流式收尾后到达），快照天然不含这些消息，全量替换会把刚出现的气泡抹掉且无
 * 后续拉取补回（reconciled 已置 true、增量补漏仅重连触发）。因此对两类未被 API
 * 覆盖（isCoveredByApi）的新鲜本地消息网开一面：
 *   1. role=user 且带 clientMessageId 且 timestamp 在新鲜度窗口内（乐观 user，
 *      发送瞬间进主数组——单一消息数组协议，无独立 pending 区）
 *   2. role=assistant 且 _lastUpdated 在新鲜度窗口内（流式占位与刚完成的回复，
 *      ensureStreamingPlaceholder/流式更新必打 _lastUpdated 戳）
 * 超窗的视为 persist 残留/断线残影，按刷新去漂移语义丢弃；被 API 覆盖的让位
 * API 权威版（不并存不重复）。游标（top/bottom）仍只按 API 权威消息计算。
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

const PIPELINE_ID = 'pipe-inflight-1'
const SESSION_ID = 'sess-inflight-1'

describe('initFromAPI 飞行中消息保留', () => {
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
  } as Message)

  /** 真实协议形态的乐观 user（handleSendMessage 直写主数组：无 _lastUpdated、status=sending） */
  const optimisticUser = (cmid: string): Message =>
    msg(cmid, 3, {
      role: 'user',
      content: '刷新后发出的消息',
      status: 'sending',
      clientMessageId: cmid,
      sequence: undefined,
    })

  /** 真实协议形态的流式占位（ensureStreamingPlaceholder：后端 a_ id + 新鲜 _lastUpdated 戳） */
  const streamingPlaceholder = (messageId: string, content: string): Message =>
    msg(messageId, undefined, {
      status: 'streaming',
      content,
      _lastUpdated: Date.now(),
    })

  /** 真实协议形态的刚完成回复（stream_end/new_message 收尾：status=completed + 新鲜 _lastUpdated 戳） */
  const justCompletedReply = (messageId: string, seq: number, content: string): Message =>
    msg(messageId, seq, {
      status: 'completed',
      content,
      _lastUpdated: Date.now(),
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
      reconciledByPipeline: {},
    })
  })

  it('核心回归：迟到 init 的快照不含刚完成的回复与乐观 user → 两条都保留、不重复', () => {
    const store = usePipelineMessageStore.getState()

    // 已有历史（rehydrate 恢复）
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: '旧问题' }),
      msg('api-asst-1', 2, { content: '旧回答' }),
    ])

    // 发送 + 回复收尾（本页活动，快照发起后才发生）
    store.addMessage(PIPELINE_ID, optimisticUser('client-uuid-ab'))
    store.addMessage(PIPELINE_ID, justCompletedReply('a_38c5e8cbe88f', 5, '任务状态还是 running，让我再等一下'))

    // 迟到的 init 响应：快照查询发生在上述消息落库前，只返回旧历史
    const staleSnapshot = [
      msg('api-user-1', 1, { role: 'user', content: '旧问题' }),
      msg('api-asst-1', 2, { content: '旧回答' }),
    ]
    store.initFromAPI(PIPELINE_ID, staleSnapshot)

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.clientMessageId === 'client-uuid-ab')).toBeDefined()
    expect(msgs.find((m) => m.id === 'a_38c5e8cbe88f')?.content).toBe('任务状态还是 running，让我再等一下')
    // 性质断言：终态无重复（每条消息 id 唯一）
    expect(new Set(msgs.map((m) => m.id)).size).toBe(msgs.length)
    // 性质断言：排序稳定非降（sequence 升序，无 seq 的飞行消息排末尾）
    const seqs = msgs.map((m) => m.sequence ?? Number.MAX_SAFE_INTEGER)
    expect([...seqs].sort((a, b) => a - b)).toEqual(seqs)
  })

  it('流式中的占位（带新鲜 _lastUpdated）在迟到 init 后保留——输出途中不被抹掉', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [msg('api-user-1', 1, { role: 'user', content: 'q' })])

    store.startStreaming(PIPELINE_ID, 'a_streaming_1')
    store.addMessage(PIPELINE_ID, streamingPlaceholder('a_streaming_1', '正在输出的半截内容'))

    // 迟到 init（快照无该消息；流式保护只在发起时检查，拦不住响应晚到）
    store.initFromAPI(PIPELINE_ID, [msg('api-user-1', 1, { role: 'user', content: 'q' })])

    const kept = usePipelineMessageStore.getState().getMessages(PIPELINE_ID).find((m) => m.id === 'a_streaming_1')
    expect(kept?.status).toBe('streaming')
    expect(kept?.content).toBe('正在输出的半截内容')
  })

  it('迟到 init 重复到达（幂等）：飞行消息只保留一份，不被快照复制', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [msg('api-user-1', 1, { role: 'user', content: 'q' })])
    store.addMessage(PIPELINE_ID, justCompletedReply('a_dup_check', 2, '回复'))

    const staleSnapshot = [msg('api-user-1', 1, { role: 'user', content: 'q' })]
    store.initFromAPI(PIPELINE_ID, staleSnapshot)
    store.initFromAPI(PIPELINE_ID, staleSnapshot)

    const replies = usePipelineMessageStore.getState().getMessages(PIPELINE_ID).filter((m) => m.id === 'a_dup_check')
    expect(replies).toHaveLength(1)
  })

  it('被 API 覆盖的飞行消息让位权威版（id/cmid 命中 → 不并存）', () => {
    const store = usePipelineMessageStore.getState()

    // 乐观 user 与 API 权威版同 cmid
    store.addMessage(PIPELINE_ID, optimisticUser('client-uuid-cd'))
    // 流式占位 id 与落库 record_id 同值（a_ id 契约）
    store.addMessage(PIPELINE_ID, streamingPlaceholder('a_covered_1', '半截'))

    store.initFromAPI(PIPELINE_ID, [
      msg('mc_record_cd', 1, { role: 'user', content: '刷新后发出的消息', clientMessageId: 'client-uuid-cd' }),
      msg('a_covered_1', 2, { content: '落库的完整回复', _lastUpdated: undefined }),
    ])

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    // cmid 收敛：只有 API 权威版 user
    const userMsgs = msgs.filter((m) => m.role === 'user')
    expect(userMsgs).toHaveLength(1)
    expect(userMsgs[0].id).toBe('mc_record_cd')
    // id 收敛：只有 API 权威版 assistant
    const asstMsgs = msgs.filter((m) => m.role === 'assistant')
    expect(asstMsgs).toHaveLength(1)
    expect(asstMsgs[0].content).toBe('落库的完整回复')
  })

  it('超窗残留仍被丢弃——刷新去漂移语义不回退', () => {
    const store = usePipelineMessageStore.getState()

    // stale 乐观 user（timestamp 2 分钟前）
    store.addMessage(PIPELINE_ID, msg('client-uuid-stale', 1, {
      role: 'user',
      content: 'stale',
      clientMessageId: 'client-uuid-stale',
      timestamp: new Date(Date.now() - 120_000).toISOString(),
    }))
    // stale streaming 残影（_lastUpdated 2 分钟前）
    store.addMessage(PIPELINE_ID, msg('stale-stream', 2, {
      status: 'streaming',
      _lastUpdated: Date.now() - 120_000,
    }))

    store.initFromAPI(PIPELINE_ID, [msg('api-1', 1, { content: 'fresh' })])

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'client-uuid-stale')).toBeUndefined()
    expect(msgs.find((m) => m.id === 'stale-stream')).toBeUndefined()
    expect(msgs).toHaveLength(1)
  })

  it('游标仍只按 API 权威消息计算，不被飞行消息污染', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: 'q' }),
      msg('api-asst-1', 2, { content: 'a' }),
    ])

    store.addMessage(PIPELINE_ID, optimisticUser('client-uuid-ef'))
    store.addMessage(PIPELINE_ID, justCompletedReply('a_cursor_check', 9, '新回复'))

    // 迟到 init：API 只有旧两条；飞行回复 seq=9 不得推进游标（after_sequence 补漏不跳空）
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: 'q' }),
      msg('api-asst-1', 2, { content: 'a' }),
    ])

    const s = usePipelineMessageStore.getState()
    expect(s.bottomCursorsByPipeline[PIPELINE_ID]).toBe(2)
    expect(s.topCursorsByPipeline[PIPELINE_ID]).toBe(1)
  })
})
