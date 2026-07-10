/**
 * 双游标补漏集成测试（完整路径）
 *
 * 本测试覆盖以前完全缺失的场景：真实的"切换会话"用户路径。
 *   首次 initFromAPI → 切走到别的会话 → 切回走 after_sequence 补漏 → 无重复
 *
 * 以前所有 store 测试都直接调 initFromAPI/addMessage，从不走 fetchMessages 的
 * after_sequence 分支（appendMessages），所以补漏路径一直是死代码、bug 测不出。
 * 本测试 mock getMessages（API 层）让 fetchMessages 真实跑 appendMessages。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

// mock apiClient.get（网络层），让 getMessages + fetchMessages + initFromAPI/appendMessages 全部真实跑
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

vi.mock('@/utils/retry', () => ({
  requestWithRetry: async (fn: () => Promise<any>) => fn(),
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

/** 设置 apiClient.get 返回的后端原始 records（会被 mapBackendMessageToMessage + merge 真实处理） */
function setApiRecords(records: any[], has_more = false) {
  mockGet.mockResolvedValueOnce({ data: { messages: records, total: records.length, has_more } })
}

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

const PIPELINE_ID = 'pipe-dual-001'
const THREAD_ID = 'thread-dual-001'

function makeMsg(id: string, seq: number, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: THREAD_ID,
    sequence: seq,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + seq * 1000).toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('双游标补漏完整路径', () => {
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
    })
  })

  it('首次 init → 切走 → 切回补漏 → 无重复（after_sequence 增量拉取）', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // 1. 首次进入：后端返回历史消息（seq 1-4）
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题1' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '回答1' },
      { id: 'u2', sequence: 3, role: 'user', content: '问题2' },
      { id: 'a2', sequence: 4, role: 'assistant', content: '回答2' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const afterInit = store.getMessages(PIPELINE_ID)
    expect(afterInit).toHaveLength(4)
    // bottomCursor 应为 4（最大 sequence），通过方法读取最新状态
    const state1 = usePipelineMessageStore.getState()
    expect(state1.getBottomCursor(PIPELINE_ID)).toBe(4)

    // 2. 切走（用户去别的会话，本管道产生新消息 seq 5-6，后端已持久化）

    // 3. 切回：已初始化（有 bottomCursor=4）→ 走 after_sequence=4 补漏
    setApiRecords([
      { id: 'u3', sequence: 5, role: 'user', content: '问题3' },
      { id: 'a3', sequence: 6, role: 'assistant', content: '回答3' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: 4 })

    const afterBackfill = store.getMessages(PIPELINE_ID)
    // ★ 核心：补漏后 6 条（4 历史 + 2 补漏），无重复
    expect(afterBackfill).toHaveLength(6)
    const ids = afterBackfill.map((m) => m.id)
    expect(ids).toEqual(['u1', 'a1', 'u2', 'a2', 'u3', 'a3'])
    // bottomCursor 推进到 6
    expect(usePipelineMessageStore.getState().getBottomCursor(PIPELINE_ID)).toBe(6)
  })

  it('首次进入（无 bottomCursor）走全量 init，不走补漏', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题1' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '回答1' },
    ])
    // 首次进入：无 after_sequence（全量 init）
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // 全量 init 后有消息 + bottomCursor 设置
    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
    expect(usePipelineMessageStore.getState().getBottomCursor(PIPELINE_ID)).toBe(2)
  })

  it('补漏后再次补漏：连续增量拉取，每次只追加新消息', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // init（seq 1-2）
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: 'q1' },
      { id: 'a1', sequence: 2, role: 'assistant', content: 'r1' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // 第一次补漏（seq 3-4）
    setApiRecords([
      { id: 'u2', sequence: 3, role: 'user', content: 'q2' },
      { id: 'a2', sequence: 4, role: 'assistant', content: 'r2' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: 2 })

    // 第二次补漏（seq 5）
    setApiRecords([
      { id: 'u3', sequence: 5, role: 'user', content: 'q3' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: 4 })

    const final = store.getMessages(PIPELINE_ID)
    // ★ 核心：5 条，无重复，顺序正确
    expect(final).toHaveLength(5)
    expect(final.map((m) => m.sequence)).toEqual([1, 2, 3, 4, 5])
  })
})
