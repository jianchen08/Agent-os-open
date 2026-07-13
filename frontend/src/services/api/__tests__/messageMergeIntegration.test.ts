/**
 * 消息合并集成测试（不 mock 核心逻辑）
 *
 * 本测试解决"测试测不出 part 级重复"的问题：
 * 1. 用真实的 mergeConsecutiveAssistantMessages（其它 store 测试都 mock 成恒等）
 * 2. 让 getMessages 真实跑 mapBackendMessageToMessage + mergeConsecutiveAssistantMessages
 * 3. 断言最终的 parts 数组（不是消息条数）没有重复 text
 *
 * 覆盖场景：
 * - 后端把一次 LLM 响应拆成 [assistant(tool_call), tool(result), assistant(text)]
 *   → 合并后应该是一个气泡，tool_call 注入 part、text 不重复
 * - 切换会话双游标路径：init → 补漏 → 最终无重复
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message, MessagePart } from '@/types/messageParts'

// mock apiClient（网络层），保留真实的 mapBackendMessageToMessage + mergeConsecutiveAssistantMessages
// session.ts 用 `import apiClient from '@/services/api/client'`（default import）
const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({
  default: { get: mockGet },
}))

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

import { getMessages } from '@/services/api/session'

/** 设置 apiClient.get 的返回数据 */
function setApiMessages(messages: any[], has_more = false) {
  mockGet.mockResolvedValue({ data: { messages, total: messages.length, has_more } })
}

const THREAD_ID = 'thread-int-001'

/** 构造后端原始 record（BackendMessageResponse 格式） */
function backendRecord(overrides: Record<string, any>): any {
  return {
    id: overrides.id || 'rec-1',
    sequence: overrides.sequence ?? 1,
    role: overrides.role || 'assistant',
    content: overrides.content || '',
    timestamp: overrides.timestamp || new Date().toISOString(),
    parentId: null,
    status: 'completed',
    ...overrides,
  }
}

describe('消息合并集成测试（真实 mergeConsecutiveAssistantMessages）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    setApiMessages([])
  })

  it('场景A: [assistant(tool_call), tool(result), assistant(text)] 合并成一个气泡，parts 不重复', async () => {
    // 后端把一次 LLM 响应拆成 3 条 record：
    // 1. assistant 带 tool_call（无 text）
    // 2. tool 结果
    // 3. assistant 带 text（最终回复）
    setApiMessages([
        backendRecord({
          id: 'rec-1', sequence: 1, role: 'assistant',
          content: '',
          toolCalls: [{ call_id: 'tc-1', tool_name: 'search', tool_args: { q: 'hello' }, result: '找到3条' }],
        }),
        backendRecord({
          id: 'rec-2', sequence: 2, role: 'tool',
          content: '找到3条',
          toolCallId: 'tc-1', toolName: 'search', toolResult: '找到3条',
        }),
        backendRecord({
          id: 'rec-3', sequence: 3, role: 'assistant',
          content: '根据搜索结果，这是回复',
        }),
    ])

    const result = await getMessages(THREAD_ID, { pipelineRunId: 'pipe-1' })

    // ★ 核心：合并后只有 1 条 assistant 消息（tool 被吸收，2 个 assistant 合并）
    const assistants = result.messages.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)

    const merged = assistants[0]
    const parts = (merged.parts || []) as MessagePart[]

    // parts 应包含：1 个 tool_call（带 result）+ 1 个 text，不重复
    const toolCallParts = parts.filter((p) => p.type === 'tool_call')
    const textParts = parts.filter((p) => p.type === 'text')
    expect(toolCallParts).toHaveLength(1)
    expect(textParts).toHaveLength(1)

    // tool_call 的 result 被正确注入
    const tcPart = toolCallParts[0] as any
    expect(tcPart.result).toBe('找到3条')

    // text 内容正确
    const textPart = textParts[0] as any
    expect(textPart.content).toBe('根据搜索结果，这是回复')
  })

  it('场景B: 多轮 assistant 回复（thinking + text + thinking + text）不应产生重复 text part', async () => {
    // 后端返回 2 条连续 assistant（各自带 thinking + text）
    setApiMessages([
        backendRecord({
          id: 'rec-1', sequence: 1, role: 'assistant',
          content: '第一段回复',
          metadata: { thinkingContent: '第一段思考' },
        }),
        backendRecord({
          id: 'rec-2', sequence: 2, role: 'assistant',
          content: '第二段回复',
          metadata: { thinkingContent: '第二段思考' },
        }),
    ])

    const result = await getMessages(THREAD_ID, { pipelineRunId: 'pipe-1' })

    // 2 条连续 assistant 合并成 1 个气泡
    const assistants = result.messages.filter((m) => m.role === 'assistant')
    expect(assistants).toHaveLength(1)

    const parts = (assistants[0].parts || []) as MessagePart[]
    const textParts = parts.filter((p) => p.type === 'text')
    const thinkingParts = parts.filter((p) => p.type === 'thinking')

    // ★ 核心：2 个不同内容的 text part 都保留（不重复，但也不丢失）
    expect(textParts).toHaveLength(2)
    expect(thinkingParts).toHaveLength(2)

    // 内容分别是第一段、第二段（不交叉、不重复）
    const textContents = textParts.map((p) => (p as any).content)
    expect(textContents).toContain('第一段回复')
    expect(textContents).toContain('第二段回复')
  })

  it('场景C: user / assistant / user / assistant 交替（不合并，各自独立）', async () => {
    setApiMessages([
        backendRecord({ id: 'u1', sequence: 1, role: 'user', content: '问题1' }),
        backendRecord({ id: 'a1', sequence: 2, role: 'assistant', content: '回答1' }),
        backendRecord({ id: 'u2', sequence: 3, role: 'user', content: '问题2' }),
        backendRecord({ id: 'a2', sequence: 4, role: 'assistant', content: '回答2' }),
    ])

    const result = await getMessages(THREAD_ID, { pipelineRunId: 'pipe-1' })

    // ★ 4 条都在，没有错误合并
    expect(result.messages).toHaveLength(4)
    const contents = result.messages.map((m) => m.content)
    expect(contents).toEqual(['问题1', '回答1', '问题2', '回答2'])
  })
})
