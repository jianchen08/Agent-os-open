/** @feature FP-T12 前端适配（截断结果展开回填） @vision V6 可即用 @ci frontend-test */
/**
 * 工具结果展开回填集成测试（数据面瘦身批次 1.2）
 *
 * mock 网络层（apiClient.get），store/mapping/backfill 全真实跑：
 * 展开截断标记 → after_sequence=seq-1 & limit=1 单条回取（旁路截断映射）
 * → 桶内截断 tool 消息按 id 替换为全量 → updatePart 回填 assistant part。
 * 防重契约：在途共享单飞、成功后不再发请求、失败可重试。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/utils/logger', async () =>
  (await import('../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull(),
)
vi.mock('@/utils/retry', async () =>
  (await import('../../stores/__tests__/helpers/storeTestMocks')).retryMockFull(),
)

const PIPE = 'pipe-backfill-001'
const THREAD = 'thread-backfill-001'

const TOOL_SEQ = 5
const FULL_RESULT = { status: 'ok', blob: 'x'.repeat(9 * 1024) }
const FULL_RESULT_STR = 'y'.repeat(9 * 1024)

/** 全量 tool 消息后端原始记录（列表端点返回全文，无截断） */
function fullToolRecord() {
  return {
    id: 't5',
    sequence: TOOL_SEQ,
    role: 'tool',
    content: FULL_RESULT_STR,
    timestamp: '2026-10-09T00:00:05Z',
    status: 'completed',
    toolCallId: 'call-1',
    toolName: 'bash_execute',
    toolResult: FULL_RESULT_STR,
    toolResultData: FULL_RESULT,
  }
}

/** 预置桶：assistant(tool_call part 带截断标记) + 截断 tool 消息 */
function truncatedToolMessage(): Message {
  return {
    id: 't5',
    sessionId: THREAD,
    sequence: TOOL_SEQ,
    role: 'tool',
    content: '截断预览',
    timestamp: '2026-10-09T00:00:05Z',
    status: 'completed',
    toolCallId: 'call-1',
    toolName: 'bash_execute',
    toolResult: { __truncated: true, preview: '截断预览', totalBytes: 99999, sequence: TOOL_SEQ },
    toolResultData: { __truncated: true, preview: '{"status":"ok","blob":"xxx', totalBytes: 99999, sequence: TOOL_SEQ },
  } as Message
}

function assistantWithMarkedPart(): Message {
  return {
    id: 'a4',
    sessionId: THREAD,
    sequence: 4,
    role: 'assistant',
    content: '',
    timestamp: '2026-10-09T00:00:04Z',
    status: 'completed',
    parts: [
      { type: 'text', content: '跑一下', state: 'done' },
      {
        type: 'tool_call',
        callId: 'call-1',
        name: 'bash_execute',
        args: { cmd: 'ls' },
        state: 'done',
        result: { __truncated: true, preview: '截断预览', totalBytes: 99999, sequence: TOOL_SEQ },
        resultData: { __truncated: true, preview: '{"status":"ok"', totalBytes: 99999, sequence: TOOL_SEQ },
      },
    ],
  } as Message
}

describe('expandTruncatedToolResult 展开回填', () => {
  let usePipelineMessageStore: any
  let expandTruncatedToolResult: typeof import('@/utils/toolResultBackfill').expandTruncatedToolResult
  let resetForTest: () => void

  beforeEach(async () => {
    vi.clearAllMocks()
    vi.resetModules()
    mockGet.mockReset()
    const store = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = store.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: { [PIPE]: THREAD },
      streamingState: {},
      activePipelineId: PIPE,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: { [PIPE]: 10 },
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
    const bf = await import('@/utils/toolResultBackfill')
    expandTruncatedToolResult = bf.expandTruncatedToolResult
    resetForTest = bf._resetToolResultBackfillForTest
    resetForTest()
  })

  function seedBucket() {
    usePipelineMessageStore.setState((s: any) => ({
      messagesByPipeline: {
        ...s.messagesByPipeline,
        [PIPE]: [assistantWithMarkedPart(), truncatedToolMessage()],
      },
    }))
  }

  it('展开 → 单条回取（after_sequence=seq-1, limit=1, 旁路截断）→ 桶内消息替换为全量 + part 回填', async () => {
    seedBucket()
    mockGet.mockResolvedValueOnce({
      data: { messages: [fullToolRecord()], total: 1, has_more: false },
    })

    const ok = await expandTruncatedToolResult({
      messageId: 'a4',
      partIndex: 1,
      callId: 'call-1',
      sequence: TOOL_SEQ,
    })
    expect(ok).toBe(true)

    // 请求形状：精确取单条 + 旁路截断
    expect(mockGet).toHaveBeenCalledTimes(1)
    const [url, config] = mockGet.mock.calls[0]
    expect(String(url)).toContain(THREAD)
    expect(config.params).toMatchObject({
      limit: 1,
      after_sequence: TOOL_SEQ - 1,
      pipeline_run_id: PIPE,
    })

    // 桶内 tool 消息已被全量版替换（标记消失）
    const msgs = usePipelineMessageStore.getState().getMessages(PIPE)
    const tool = msgs.find((m: Message) => m.id === 't5')!
    expect(tool.toolResult).toBe(FULL_RESULT_STR)
    expect(tool.toolResultData).toEqual(FULL_RESULT)

    // assistant part 回填全量
    const assistant = msgs.find((m: Message) => m.id === 'a4')!
    const part = assistant.parts![1] as any
    expect(part.result).toBe(FULL_RESULT_STR)
    expect(part.resultData).toEqual(FULL_RESULT)

    // 游标不被历史单条回取污染（bottomCursor 保持 max）
    expect(usePipelineMessageStore.getState().getBottomCursor(PIPE)).toBe(10)
  })

  it('已回填的键不再发请求（每次展开至多一次）', async () => {
    seedBucket()
    mockGet.mockResolvedValue({
      data: { messages: [fullToolRecord()], total: 1, has_more: false },
    })
    const params = { messageId: 'a4', partIndex: 1, callId: 'call-1', sequence: TOOL_SEQ }
    await expandTruncatedToolResult(params)
    expect(mockGet).toHaveBeenCalledTimes(1)
    await expect(expandTruncatedToolResult(params)).resolves.toBe(true)
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('在途并发共享单飞：两次展开只发一次请求', async () => {
    seedBucket()
    let resolveApi: (v: unknown) => void = () => {}
    mockGet.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveApi = resolve
      }),
    )
    const params = { messageId: 'a4', partIndex: 1, callId: 'call-1', sequence: TOOL_SEQ }
    const first = expandTruncatedToolResult(params)
    const second = expandTruncatedToolResult(params)
    await Promise.resolve()
    resolveApi({ data: { messages: [fullToolRecord()], total: 1, has_more: false } })
    await Promise.all([first, second])
    expect(mockGet).toHaveBeenCalledTimes(1)
  })

  it('回取失败 → false（可重试），重试成功 → true', async () => {
    seedBucket()
    mockGet.mockRejectedValueOnce(new Error('network down'))
    const params = { messageId: 'a4', partIndex: 1, callId: 'call-1', sequence: TOOL_SEQ }
    await expect(expandTruncatedToolResult(params)).resolves.toBe(false)
    expect(mockGet).toHaveBeenCalledTimes(1)

    // 失败不落已回填标记：重试再次发请求并成功
    mockGet.mockResolvedValueOnce({
      data: { messages: [fullToolRecord()], total: 1, has_more: false },
    })
    await expect(expandTruncatedToolResult(params)).resolves.toBe(true)
    expect(mockGet).toHaveBeenCalledTimes(2)
    const tool = usePipelineMessageStore.getState().getMessages(PIPE).find((m: Message) => m.id === 't5')!
    expect(tool.toolResultData).toEqual(FULL_RESULT)
  })

  it('sequence 缺失（流式占位未挂 seq）→ 如实失败，不发请求', async () => {
    seedBucket()
    const ok = await expandTruncatedToolResult({ messageId: 'a4', partIndex: 1, callId: 'call-1' })
    expect(ok).toBe(false)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('取回消息对不上（无同 sequence 的 tool 消息）→ 如实失败', async () => {
    seedBucket()
    mockGet.mockResolvedValueOnce({
      data: { messages: [{ ...fullToolRecord(), sequence: 6 }], total: 1, has_more: false },
    })
    const ok = await expandTruncatedToolResult({
      messageId: 'a4',
      partIndex: 1,
      callId: 'call-1',
      sequence: TOOL_SEQ,
    })
    expect(ok).toBe(false)
  })

  it('独立 tool 消息卡（无 partIndex）→ 只做桶内替换即成功', async () => {
    seedBucket()
    mockGet.mockResolvedValueOnce({
      data: { messages: [fullToolRecord()], total: 1, has_more: false },
    })
    await expect(
      expandTruncatedToolResult({ messageId: 't5', callId: 'call-1', sequence: TOOL_SEQ }),
    ).resolves.toBe(true)
    const tool = usePipelineMessageStore.getState().getMessages(PIPE).find((m: Message) => m.id === 't5')!
    expect(tool.toolResultData).toEqual(FULL_RESULT)
  })
})
