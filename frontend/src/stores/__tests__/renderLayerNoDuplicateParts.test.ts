/**
 * 渲染层防线集成测试：store 最终消息的 parts 数组无重复 text
 *
 * 这是直接对应"切换会话时结尾片段重复"症状的测试。
 * 以前所有测试只检查消息条数/id，不检查 parts 内容——所以 part 级重复测不出。
 * 本测试检查 store 最终状态的 parts 数组，模拟用户真实看到的渲染数据。
 *
 * 覆盖：后端返回拆分 record → map+merge → store 最终消息 → 每条消息的 text parts 无重复
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// mock apiClient（网络层），让 mapBackendMessageToMessage + mergeConsecutiveAssistantMessages 真实跑
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

const PIPELINE_ID = 'pipe-render-001'
const THREAD_ID = 'thread-render-001'

describe('渲染层防线：store 最终 parts 无重复 text', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    const mod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = mod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {}, activePipelineId: null,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    mockGet.mockReset()
  })

  /** 设置 apiClient.get 返回后端原始 records */
  function setApiRecords(records: any[], has_more = false) {
    mockGet.mockResolvedValueOnce({ data: { messages: records, total: records.length, has_more } })
  }

  /**
   * 断言 store 中所有消息的 text parts 无重复内容。
   * 这是渲染层防线的核心：buildFragmentsFromParts 把 text part 转成 fragment 渲染，
   * 如果同一条消息有重复 text part，就会渲染出重复片段（用户看到的"结尾重复"）。
   */
  function assertNoDuplicateTextParts(label: string) {
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    for (const msg of msgs) {
      const textParts = (msg.parts || []).filter((p: any) => p.type === 'text')
      const contents = textParts.map((p: any) => (p.content || '').trim()).filter(Boolean)
      const seen = new Set<string>()
      for (const c of contents) {
        if (seen.has(c)) {
          throw new Error(
            `[${label}] 消息 ${msg.id} 有重复 text part: "${c.slice(0, 30)}..."，` +
            `这会导致渲染层把同一段文本渲染两次`,
          )
        }
        seen.add(c)
      }
    }
  }

  it('场景A: 后端把一次回复拆成 [assistant(tool_call), tool(result), assistant(text)] → 最终 parts 无重复', async () => {
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    setApiRecords([
      { id: 'rec-1', sequence: 1, role: 'assistant', content: '',
        toolCalls: [{ call_id: 'tc-1', tool_name: 'search', tool_args: {}, result: '结果' }] },
      { id: 'rec-2', sequence: 2, role: 'tool', content: '结果',
        toolCallId: 'tc-1', toolName: 'search', toolResult: '结果' },
      { id: 'rec-3', sequence: 3, role: 'assistant', content: '这是最终回复' },
    ])
    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // ★ 渲染层防线：最终 parts 无重复 text
    expect(() => assertNoDuplicateTextParts('场景A')).not.toThrow()

    // 进一步验证：合并后只有 1 个 text part
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const assistants = msgs.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)
    const textParts = (assistants[0].parts || []).filter((p: any) => p.type === 'text')
    expect(textParts).toHaveLength(1)
  })

  it('场景B: 多轮 thinking+text → 最终每条消息的 text parts 无重复', async () => {
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题' },
      { id: 'a1', sequence: 2, role: 'assistant', content: '第一段',
        metadata: { thinkingContent: '思考1' } },
      { id: 'a2', sequence: 3, role: 'assistant', content: '第二段',
        metadata: { thinkingContent: '思考2' } },
    ])
    await usePipelineMessageStore.getState().fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    // ★ 渲染层防线
    expect(() => assertNoDuplicateTextParts('场景B')).not.toThrow()
  })

  it('场景C: 切换会话（init→补漏）后最终 parts 无重复', async () => {
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // 首次 init：含 tool_call 的回复
    setApiRecords([
      { id: 'u1', sequence: 1, role: 'user', content: '问题1' },
      { id: 'a1-1', sequence: 2, role: 'assistant', content: '',
        toolCalls: [{ call_id: 'tc-1', tool_name: 'run', tool_args: {}, result: 'ok' }] },
      { id: 't1', sequence: 3, role: 'tool', content: 'ok',
        toolCallId: 'tc-1', toolName: 'run', toolResult: 'ok' },
      { id: 'a1-2', sequence: 4, role: 'assistant', content: '回复1' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID })

    const bottomCursor = usePipelineMessageStore.getState().getBottomCursor(PIPELINE_ID)

    // 补漏：新的 tool_call 回复
    setApiRecords([
      { id: 'u2', sequence: 5, role: 'user', content: '问题2' },
      { id: 'a2-1', sequence: 6, role: 'assistant', content: '',
        toolCalls: [{ call_id: 'tc-2', tool_name: 'run', tool_args: {}, result: 'ok2' }] },
      { id: 't2', sequence: 7, role: 'tool', content: 'ok2',
        toolCallId: 'tc-2', toolName: 'run', toolResult: 'ok2' },
      { id: 'a2-2', sequence: 8, role: 'assistant', content: '回复2' },
    ])
    await store.fetchMessages(PIPELINE_ID, { threadId: THREAD_ID, after_sequence: bottomCursor })

    // ★ 渲染层防线：补漏后所有消息的 text parts 无重复
    expect(() => assertNoDuplicateTextParts('场景C-补漏后')).not.toThrow()

    // 验证补漏成功（消息数增加，无重复 id）
    const final = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const ids = final.map((m) => m.id)
    expect(new Set(ids).size).toBe(ids.length) // 无重复 id
  })
})
