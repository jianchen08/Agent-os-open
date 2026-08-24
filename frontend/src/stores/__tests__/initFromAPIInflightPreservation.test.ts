/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * initFromAPI 飞行中乐观消息保留测试（回归：2026-08-20 用户真实反馈）。
 *
 * Bug 现象：刷新后发送的第一条消息，"思考中"气泡出现 1-2s 后消失，
 * 消息列表被替换成之前的旧气泡——用户输入凭空丢失。
 *
 * 根因：会话恢复的 loadPipelineMessages(mode=init) 响应（后端全量读，大会话
 * 10-40s）晚于用户发送时，initFromAPI「全量替换、不保留本地」把飞行中的
 * 乐观 user 消息（clientMessageId 未对账）与流式占位（placeholder_*，
 * _lastUpdated 新鲜）一起冲掉；后端若尚未收到/落库该消息，WS 补漏也无法
 * 找回，输入即永久丢失。
 *
 * 契约（修复后）：initFromAPI 的刷新语义仍是「API 权威替换」，但对两类
 * 明确的飞行中本地消息网开一面：
 *   1. role=user 且带 clientMessageId 且未被 API 对账覆盖（isCoveredByApi）
 *   2. status=streaming 且 _lastUpdated 在 90s 内（发送瞬间创建的占位；
 *      persist 残留的 stale streaming 无新鲜 _lastUpdated，照旧丢弃）
 * 游标（top/bottom）仍只按 API 权威消息计算，不被乐观 sequence 污染。
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

describe('initFromAPI 飞行中乐观消息保留', () => {
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

  it('核心场景：迟到 init 全量替换，store 残影让位（发送保护由 pending 区承担，ADR 2026-08-21）', () => {
    const store = usePipelineMessageStore.getState()

    // 已有历史（rehydrate 恢复 + 首次 init 完成）
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: '旧问题' }),
      msg('api-asst-1', 2, { content: '旧回答' }),
    ])

    // 旧架构残影：乐观 user 与占位曾直接写入主 store（新架构分别由 pending 区
    // 与 stream_start 的真实 message_id 占位承担，此处入 store 验证替换语义）
    store.addMessage(PIPELINE_ID, msg('client-uuid-ab', 3, {
      role: 'user',
      content: '刷新后的第一条消息',
      status: 'completed',
      clientMessageId: 'client-uuid-ab',
    }))
    store.startStreaming(PIPELINE_ID, 'placeholder_xyz')
    store.addMessage(PIPELINE_ID, msg('placeholder_xyz', 4, {
      status: 'streaming',
      _lastUpdated: Date.now(),
    }))

    // 迟到的 init 响应：后端尚未落库新消息，只返回旧历史
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: '旧问题' }),
      msg('api-asst-1', 2, { content: '旧回答' }),
    ])

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    // 契约（2026-08-21 ADR）：API 权威全量替换——store 残影（乐观 user/占位）
    // 一律让位，杜绝幽灵气泡复活。"用户输入不消失"由 pending 区承担：
    // 真实发送时乐观消息不在 store，init 无从冲掉（pendingMessageLifecycle.test.ts）。
    expect(msgs.find((m) => m.clientMessageId === 'client-uuid-ab')).toBeUndefined()
    expect(msgs.find((m) => m.id === 'placeholder_xyz')).toBeUndefined()
    expect(msgs.map((m) => m.sequence)).toEqual([1, 2])
  })

  it('stale streaming 残留（_lastUpdated 超过 90s）仍被丢弃——刷新去漂移语义不回退', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, msg('stale-stream', 1, {
      status: 'streaming',
      _lastUpdated: Date.now() - 10 * 60 * 1000,
    }))
    store.initFromAPI(PIPELINE_ID, [msg('api-1', 1, { content: 'fresh' })])

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'stale-stream')).toBeUndefined()
    expect(msgs).toHaveLength(1)
  })

  it('乐观 user 已被 API 对账覆盖时让位 API 版（增量/全量终态一致）', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, msg('client-uuid-cd', 1, {
      role: 'user',
      content: 'hello',
      clientMessageId: 'client-uuid-cd',
    }))
    store.initFromAPI(PIPELINE_ID, [
      msg('server-record-1', 1, {
        role: 'user',
        content: 'hello',
        clientMessageId: 'client-uuid-cd',
      }),
    ])

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const userMsgs = msgs.filter((m) => m.role === 'user')
    expect(userMsgs).toHaveLength(1)
    expect(userMsgs[0].id).toBe('server-record-1')
  })

  it('游标仍按 API 权威消息计算，不被乐观 sequence 污染', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: 'q' }),
      msg('api-asst-1', 2, { content: 'a' }),
    ])
    store.addMessage(PIPELINE_ID, msg('client-uuid-ef', 3, {
      role: 'user',
      content: 'inflight',
      clientMessageId: 'client-uuid-ef',
    }))
    store.startStreaming(PIPELINE_ID, 'placeholder_gg')
    store.addMessage(PIPELINE_ID, msg('placeholder_gg', 4, {
      status: 'streaming',
      _lastUpdated: Date.now(),
    }))

    // 迟到 init：API 只有旧两条
    store.initFromAPI(PIPELINE_ID, [
      msg('api-user-1', 1, { role: 'user', content: 'q' }),
      msg('api-asst-1', 2, { content: 'a' }),
    ])

    const s = usePipelineMessageStore.getState()
    expect(s.bottomCursorsByPipeline[PIPELINE_ID]).toBe(2)
    expect(s.topCursorsByPipeline[PIPELINE_ID]).toBe(1)
  })
})
