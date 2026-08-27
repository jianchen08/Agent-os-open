/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * transient_states 存活中间态 → 流式占位重建契约（ADR 2026-08-27 §2.6 前端刷新恢复）。
 *
 * 契约：F5 刷新后消息读取接口带出寄存器中未落库的流式中间态
 * （`transient_states: [{key: "chunk:<message_id>", value: {text_len,
 * reasoning_len, blocks:[{type,content}]}}]`）——接口返回它 = 该消息尚未落库
 * （流式进行中）。fetchMessages init 路径据此重建流式占位气泡：
 *   1. 形状与 ensureStreamingPlaceholder（services/websocket/streaming/handlers/
 *      utils.ts）产出的流式占位同构：id=message_id、role='assistant'、
 *      status='streaming'、content=text 块拼接、无 parts、sequence 挂空、
 *      _lastUpdated 打新鲜戳——后续 chunk 到达按 id 命中续写，new_message
 *      合并（preferServer）不产生前缀重复。
 *   2. 幂等：同 id 已存在（WS 先到/INFLIGHT 保留）→ 跳过；API 消息列表已含
 *      同 id 权威消息（落库完成）→ 跳过。
 *   3. 占位走 addMessage（单一消息数组协议），不推进 bottomCursor（与流式
 *      占位同规则；权威 seq 由 stream_end final_sequence / new_message 对账纠正）。
 *   4. 无 transient_states → 行为与现状完全一致（回归保护）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

const { mockGetMessages } = vi.hoisted(() => ({ mockGetMessages: vi.fn() }))
vi.mock('@/services/api/session', () => ({
  getMessages: mockGetMessages,
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

const PIPELINE_ID = 'pipe-transient-1'
const THREAD_ID = 'thread-transient-1'

/** 后端原始消息 record（与 loadPipelineMessages.test.ts 同构） */
function apiRecord(id: string, seq: number, overrides: Record<string, unknown> = {}) {
  return {
    id,
    sequence: seq,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    ...overrides,
  }
}

/** 后端 transient_states 条目（内核 chunk 累积快照形状，transient.rs） */
function chunkState(messageId: string, text: string, extra: Record<string, unknown> = {}) {
  return {
    key: `chunk:${messageId}`,
    value: {
      text_len: text.length,
      reasoning_len: 0,
      blocks: [{ type: 'text', content: text }],
      ...extra,
    },
  }
}

describe('transient_states 流式占位重建', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  it('API 含 transient_states → 重建流式占位，形状与 ensureStreamingPlaceholder 同构', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [
        apiRecord('u1', 1, { role: 'user', content: 'q' }),
        apiRecord('a1', 2, { content: '旧回答' }),
      ],
      total: 2,
      has_more: false,
      transient_states: [chunkState('a_streaming_1', '正在输出的半截内容')],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const placeholder = msgs.find((m) => m.id === 'a_streaming_1')
    expect(placeholder).toBeDefined()
    // 形状同构断言（对照 ensureStreamingPlaceholder 产出字段集）
    expect(placeholder?.role).toBe('assistant')
    expect(placeholder?.status).toBe('streaming')
    expect(placeholder?.content).toBe('正在输出的半截内容')
    expect(placeholder?.sessionId).toBe(THREAD_ID)
    expect(placeholder?.parentId).toBeNull()
    expect(placeholder?.sequence).toBeUndefined()
    expect(placeholder?.parts).toBeUndefined()
    expect(typeof placeholder?._lastUpdated).toBe('number')
    // 新鲜度窗口内（INFLIGHT_FRESH_MS=90s 内，迟到 init 不抹掉）
    expect(Date.now() - (placeholder?._lastUpdated ?? 0)).toBeLessThan(90_000)
    // 性质断言：终态无重复（每条消息 id 唯一）
    expect(new Set(msgs.map((m) => m.id)).size).toBe(msgs.length)
  })

  it('多 text 块按序拼接；非 chunk: 键（progress 等）不构造占位', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [apiRecord('u1', 1, { role: 'user', content: 'q' })],
      total: 1,
      has_more: false,
      transient_states: [
        {
          key: 'chunk:a_multi_1',
          value: {
            text_len: 6,
            reasoning_len: 0,
            blocks: [
              { type: 'text', content: '第一段' },
              { type: 'text', content: '第二段' },
            ],
          },
        },
        { key: 'progress:1', value: { pct: 40 } },
      ],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'a_multi_1')?.content).toBe('第一段第二段')
    // 非 chunk 键不产生消息占位
    expect(msgs.some((m) => m.id === '1' || m.id === 'progress:1')).toBe(false)
  })

  it('幂等：API 消息列表已含同 id 权威消息 → 不构造占位（让位权威版）', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [
        apiRecord('u1', 1, { role: 'user', content: 'q' }),
        // 同 id 权威消息已落库（流式已结束，寄存器键尚未清的理论窗口）
        apiRecord('a_covered_1', 2, { content: '落库的完整回复', status: 'completed' }),
      ],
      total: 2,
      has_more: false,
      transient_states: [chunkState('a_covered_1', '半截内容')],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const target = msgs.filter((m) => m.id === 'a_covered_1')
    expect(target).toHaveLength(1)
    // 权威版保留，占位不覆盖
    expect(target[0].content).toBe('落库的完整回复')
    expect(target[0].status).toBe('completed')
  })

  it('幂等：本地已存在同 id（WS 先到/INFLIGHT 保留）→ 跳过不覆盖不双插', async () => {
    // WS 先到：本地已有流式占位（含已累积内容）
    usePipelineMessageStore.getState().addMessage(PIPELINE_ID, {
      id: 'a_ws_first_1',
      sessionId: THREAD_ID,
      role: 'assistant',
      content: 'WS 已累积的更新内容',
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'streaming',
      _lastUpdated: Date.now(),
    } as Message)

    mockGetMessages.mockResolvedValueOnce({
      messages: [apiRecord('u1', 1, { role: 'user', content: 'q' })],
      total: 1,
      has_more: false,
      transient_states: [chunkState('a_ws_first_1', '快照旧内容')],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const target = msgs.filter((m) => m.id === 'a_ws_first_1')
    expect(target).toHaveLength(1)
    // 本地内容不被快照覆盖（快照可能落后于 WS 实时累积）
    expect(target[0].content).toBe('WS 已累积的更新内容')
  })

  it('幂等：重复 fetch（同 transient_states 响应）→ 占位只构造一份', async () => {
    const response = {
      messages: [apiRecord('u1', 1, { role: 'user', content: 'q' })],
      total: 1,
      has_more: false,
      transient_states: [chunkState('a_dup_1', '半截')],
    }
    mockGetMessages.mockResolvedValueOnce(response)
    mockGetMessages.mockResolvedValueOnce(response)

    const store = usePipelineMessageStore.getState()
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.filter((m) => m.id === 'a_dup_1')).toHaveLength(1)
  })

  it('占位不推进 bottomCursor（与流式占位同规则，权威 seq 由对账纠正）', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [
        apiRecord('u1', 1, { role: 'user', content: 'q' }),
        apiRecord('a1', 2, { content: '旧回答' }),
      ],
      total: 2,
      has_more: false,
      transient_states: [chunkState('a_streaming_2', '半截')],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const s = usePipelineMessageStore.getState()
    // 游标只按 API 权威消息计算（占位无权威 seq，不得污染 after_sequence 补漏窗口）
    expect(s.bottomCursorsByPipeline[PIPELINE_ID]).toBe(2)
    expect(s.topCursorsByPipeline[PIPELINE_ID]).toBe(1)
  })

  it('回归保护：无 transient_states → 行为与现状完全一致（不构造占位）', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [
        apiRecord('u1', 1, { role: 'user', content: 'q' }),
        apiRecord('a1', 2, { content: '旧回答' }),
      ],
      total: 2,
      has_more: false,
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(2)
    expect(msgs.some((m) => m.status === 'streaming')).toBe(false)
    const s = usePipelineMessageStore.getState()
    expect(s.bottomCursorsByPipeline[PIPELINE_ID]).toBe(2)
    expect(s.topCursorsByPipeline[PIPELINE_ID]).toBe(1)
  })

  it('回归保护：transient_states 为空数组 → 不构造占位', async () => {
    mockGetMessages.mockResolvedValueOnce({
      messages: [apiRecord('u1', 1, { role: 'user', content: 'q' })],
      total: 1,
      has_more: false,
      transient_states: [],
    })

    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(1)
    expect(msgs.some((m) => m.status === 'streaming')).toBe(false)
  })
})
