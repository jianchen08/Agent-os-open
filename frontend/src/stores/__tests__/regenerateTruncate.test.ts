/**
 * 重新生成/回退的乐观截断测试（批次 D/E 前端部分）。
 *
 * 行为契约（docs/working/聊天中断保留与重新生成回退方案_20260826.md §二.3/4/5）：
 *  - truncateMessagesAfter(pipelineId, userMessageId)：保留到目标 user 消息（含），
 *    其后消息整体移除——截断点只能是 user 消息边界（tool_calls/tool 配对完整性）；
 *  - findLastUserMessageId：返回最后一条 user 消息（重新生成缺省目标）。
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
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pipe-reg-001'
const SESSION_ID = 'sess-reg-001'

describe('truncateMessagesAfter / findLastUserMessageId（重新生成/回退乐观截断）', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore

  const makeMsg = (id: string, role: Message['role'], seq: number, overrides: Partial<Message> = {}): Message => ({
    id,
    sessionId: SESSION_ID,
    sequence: seq,
    role,
    content: role === 'user' ? `问题${seq}` : `回答${seq}`,
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
      pipelineSessionMap: { [PIPELINE_ID]: SESSION_ID },
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
  })

  it('截断：保留到目标 user 消息（含），其后 assistant/tool 整体移除', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, makeMsg('u1', 'user', 1))
    store.addMessage(PIPELINE_ID, makeMsg('a1', 'assistant', 2))
    store.addMessage(PIPELINE_ID, makeMsg('t1', 'tool', 3))
    store.addMessage(PIPELINE_ID, makeMsg('u2', 'user', 4))
    store.addMessage(PIPELINE_ID, makeMsg('a2', 'assistant', 5))

    store.truncateMessagesAfter(PIPELINE_ID, 'u2')

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.map((m) => m.id)).toEqual(['u1', 'a1', 't1', 'u2'])
    // 配对完整性：截断后 tool 结果与其 assistant.tool_calls 仍成对
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(2)
  })

  it('截断到更早 user（回退）：该消息之后整体截断', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, makeMsg('u1', 'user', 1))
    store.addMessage(PIPELINE_ID, makeMsg('a1', 'assistant', 2))
    store.addMessage(PIPELINE_ID, makeMsg('u2', 'user', 3))
    store.addMessage(PIPELINE_ID, makeMsg('a2', 'assistant', 4))

    store.truncateMessagesAfter(PIPELINE_ID, 'u1')

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs.map((m) => m.id)).toEqual(['u1'])
  })

  it('未知目标 user 消息 → 幂等跳过（列表不变）', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, makeMsg('u1', 'user', 1))
    store.addMessage(PIPELINE_ID, makeMsg('a1', 'assistant', 2))

    store.truncateMessagesAfter(PIPELINE_ID, 'not-exist')

    expect(store.getMessages(PIPELINE_ID)).toHaveLength(2)
  })

  it('findLastUserMessageId：返回最后一条 user（跳过其后的 assistant/tool）', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, makeMsg('u1', 'user', 1))
    store.addMessage(PIPELINE_ID, makeMsg('a1', 'assistant', 2))
    store.addMessage(PIPELINE_ID, makeMsg('t1', 'tool', 3))
    store.addMessage(PIPELINE_ID, makeMsg('u2', 'user', 4))
    store.addMessage(PIPELINE_ID, makeMsg('a2', 'assistant', 5))

    expect(store.findLastUserMessageId(PIPELINE_ID)).toBe('u2')
  })

  it('findLastUserMessageId：无 user 消息返回 null', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE_ID, makeMsg('a1', 'assistant', 1))
    expect(store.findLastUserMessageId(PIPELINE_ID)).toBeNull()
  })
})
