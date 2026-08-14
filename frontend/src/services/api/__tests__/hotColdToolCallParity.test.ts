/**
 * 冷热路径数据结构一致性契约（TDD）：
 *
 * 同一条工具调用，无论何时读取，产出的 tool_call part 数据结构必须一致——
 *   热（实时）：WS tool_start/tool_result 事件 → pipelineMessageStore.parts[]
 *   冷（刷新）：SQLite 持久化 → GET /threads/{id}/messages → mapBackendMessageToMessage
 *              → mergeConsecutiveAssistantMessages → parts[]
 *
 * 契约字段（双侧断言相同值/类型）：
 *   callId / name / state / args（对象，深相等）/ result（全量文本，>200 字符证明无截断）
 *   / resultData（结构化数据，深相等）/ durationMs / error / containerTaskId
 *
 * 热侧事件 fixture 镜像后端 tool_core emit_tool_event 的 payload 契约（result 全量、
 * 携带 result_data/success/duration_ms）；冷侧 fixture 镜像修复后 HTTP handler 的返回
 * （toolCalls OpenAI 格式 + tool 消息 status/error/toolCallId/toolResultData/toolDurationMs）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

// 冷加载路径走真实 session.ts（mapBackendMessageToMessage + merge），只 mock HTTP 层。
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  requestWithRetry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: { getState: () => ({ clear: vi.fn(), set: vi.fn(), get: () => null }) },
}))

vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: vi.fn() }) },
}))

// ── 共享 fixture（同一份工具调用在两条路径上的两种形态） ─────────────────────
const PID = 'pipe-parity-001'
const SID = 'sess-parity-001'
const MSG_ID = 'msg-assistant-001'
const CALL_ID = 'call_parity_001'
const TOOL_NAME = 'file_write'
const CTID = 'task_ctid_001'
const T = '2026-08-14T10:00:00Z'

const ARGS = { file_path: 'src/main.rs', content: 'fn main() {}\n' }

const RESULT_DATA = {
  added: 2,
  removed: 0,
  lines: 1,
  old_content: '',
  new_content: 'fn main() {}\n',
}

// >200 字符：任何一侧存在截断（历史 bug：实时事件 take(200)）都会破坏 parity。
const RESULT_TEXT = [
  'added: 2',
  'lines: 1',
  'new_content: |',
  '  fn main() {}',
  'old_content: \'\'',
  `output: ${'lorem ipsum dolor sit amet '.repeat(20)}`,
].join('\n')

const DURATION_MS = 123.4

const ERROR_TEXT = 'boom'

/** 热侧：tool_start 事件 payload（镜像 tool_core emit_tool_event）。 */
function hotToolStartEvent() {
  return {
    thread_id: SID,
    pipeline_id: PID,
    message_id: MSG_ID,
    call_id: CALL_ID,
    tool_name: TOOL_NAME,
    args: ARGS,
    container_task_id: CTID,
  }
}

/** 热侧：tool_result 事件 payload（result 全量文本 + result_data 结构化）。 */
function hotToolResultEvent() {
  return {
    thread_id: SID,
    pipeline_id: PID,
    message_id: MSG_ID,
    call_id: CALL_ID,
    tool_name: TOOL_NAME,
    result: RESULT_TEXT,
    result_data: RESULT_DATA,
    success: true,
    duration_ms: DURATION_MS,
  }
}

/** 冷侧：assistant 消息（toolCalls 为 OpenAI 格式，arguments 是 JSON 字符串）。 */
function coldAssistantMessage() {
  return {
    id: 'm_pid_2',
    thread_id: SID,
    sequence: 2,
    role: 'assistant',
    content: '',
    timestamp: T,
    status: 'completed',
    toolCalls: [
      {
        id: CALL_ID,
        type: 'function',
        function: { name: TOOL_NAME, arguments: JSON.stringify(ARGS) },
      },
    ],
  }
}

/** 冷侧：tool 消息（content=持久化全文，结构化字段来自 tool_result_json 投影）。 */
function coldToolMessage() {
  return {
    id: 'm_pid_3',
    thread_id: SID,
    sequence: 3,
    role: 'tool',
    content: RESULT_TEXT,
    timestamp: T,
    status: 'completed',
    toolCallId: CALL_ID,
    toolName: TOOL_NAME,
    toolResultData: RESULT_DATA,
    toolDurationMs: DURATION_MS,
    containerTaskId: CTID,
  }
}

/** 契约断言：对任意一条路径产出的 tool_call part 校验同一结构。 */
function expectToolCallPartContract(part: any, overrides?: { state?: string; error?: string; result?: unknown; resultData?: unknown }) {
  expect(part, 'tool_call part 必须存在').toBeTruthy()
  expect(part.type).toBe('tool_call')
  expect(part.callId).toBe(CALL_ID)
  expect(part.name).toBe(TOOL_NAME)
  // args 必须是对象（冷侧历史 bug：function.arguments 字符串被原样透传）
  expect(part.args, 'args 必须是对象而非 JSON 字符串').toBeTypeOf('object')
  expect(part.args).toEqual(ARGS)
  // result 全量文本（双侧同源；>200 字符防截断回退）
  expect(part.result).toBe(overrides && 'result' in overrides ? overrides.result : RESULT_TEXT)
  // resultData 结构化数据深相等（冷侧历史 bug：从未映射 → 刷新后 diff 徽标丢失）。
  // 失败工具双侧统一为 undefined（热侧 ?? 语义 + 冷侧 null 归一）。
  expect(part.resultData).toEqual(overrides && 'resultData' in overrides ? overrides.resultData : RESULT_DATA)
  expect(part.durationMs).toBe(DURATION_MS)
  expect(part.state).toBe(overrides?.state ?? 'done')
  expect(part.error).toBe(overrides?.error ?? undefined)
  expect(part.containerTaskId).toBe(CTID)
}

describe('工具调用冷热路径数据结构一致性', () => {
  let usePipelineMessageStore: any

  beforeEach(async () => {
    vi.resetModules()
    mockGet.mockReset()

    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    // 热侧前置：活跃管道 + assistant 占位消息（stream_start 语义）
    usePipelineMessageStore.setState({
      messagesByPipeline: {
        [PID]: [
          { id: MSG_ID, role: 'assistant', status: 'streaming', content: '', parts: [], sequence: 1, timestamp: T },
        ],
      },
      pipelines: { [PID]: { pipelineRunId: PID, sessionId: SID } },
      pipelineSessionMap: { [PID]: SID },
      streamingState: {},
      activePipelineId: PID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  /** 热侧构造：真实 handleToolStart/handleToolResult 写入真实 store。 */
  async function buildHotPart(): Promise<any> {
    const { handleToolStart, handleToolResult } = await import(
      '@/services/websocket/streaming/handlers/toolHandler'
    )
    handleToolStart(hotToolStartEvent())
    handleToolResult(hotToolResultEvent())
    const msgs = usePipelineMessageStore.getState().messagesByPipeline[PID]
    const assistant = msgs.find((m: any) => m.id === MSG_ID)
    return (assistant?.parts || []).find((p: any) => p.type === 'tool_call')
  }

  /** 冷侧构造：mock HTTP → 真实 getMessages 映射 → 真实 merge。 */
  async function buildColdParts(backendMessages: any[]): Promise<any[]> {
    mockGet.mockResolvedValue({ data: { messages: backendMessages, total: backendMessages.length, has_more: false } })
    const { getMessages, mergeConsecutiveAssistantMessages } = await import('@/services/api/session')
    const res = await getMessages(SID, { pipelineRunId: PID })
    const merged = mergeConsecutiveAssistantMessages(res.messages)
    const assistant = merged.find(
      (m: any) => m.role === 'assistant' && (m.parts || []).some((p: any) => p.type === 'tool_call'),
    )
    return (assistant?.parts || []).filter((p: any) => p.type === 'tool_call')
  }

  it('成功工具调用：热路径 part 满足契约', async () => {
    const hotPart = await buildHotPart()
    expectToolCallPartContract(hotPart)
  })

  it('成功工具调用：冷路径 part 与热路径结构一致', async () => {
    const [hotPart, coldParts] = await Promise.all([
      buildHotPart(),
      buildColdParts([coldAssistantMessage(), coldToolMessage()]),
    ])
    expect(coldParts.length).toBe(1)
    expectToolCallPartContract(coldParts[0])
    // 双侧逐字段相等（结构一致性的直接表达）
    expect(coldParts[0].callId).toBe(hotPart.callId)
    expect(coldParts[0].name).toBe(hotPart.name)
    expect(coldParts[0].state).toBe(hotPart.state)
    expect(coldParts[0].args).toEqual(hotPart.args)
    expect(coldParts[0].result).toBe(hotPart.result)
    expect(coldParts[0].resultData).toEqual(hotPart.resultData)
    expect(coldParts[0].durationMs).toBe(hotPart.durationMs)
    expect(coldParts[0].containerTaskId).toBe(hotPart.containerTaskId)
  })

  it('失败工具调用：双侧 state=error 且 error 文本一致', async () => {
    // 热：success=false + error 文本；result 为与持久化同源的 "Error: {error}" 全文
    const hotPart = await (async () => {
      const { handleToolStart, handleToolResult } = await import(
        '@/services/websocket/streaming/handlers/toolHandler'
      )
      handleToolStart(hotToolStartEvent())
      handleToolResult({
        ...hotToolResultEvent(),
        result: `Error: ${ERROR_TEXT}`,
        result_data: null,
        success: false,
        duration_ms: DURATION_MS,
        error: ERROR_TEXT,
      })
      const msgs = usePipelineMessageStore.getState().messagesByPipeline[PID]
      const assistant = msgs.find((m: any) => m.id === MSG_ID)
      return (assistant?.parts || []).find((p: any) => p.type === 'tool_call')
    })()

    expectToolCallPartContract(hotPart, {
      state: 'error',
      error: ERROR_TEXT,
      result: `Error: ${ERROR_TEXT}`,
      resultData: undefined,
    })

    // 冷：status=failed + error 列 + content="Error: {error}"
    const coldParts = await buildColdParts([
      coldAssistantMessage(),
      {
        ...coldToolMessage(),
        content: `Error: ${ERROR_TEXT}`,
        status: 'failed',
        error: ERROR_TEXT,
        toolResultData: null,
      },
    ])
    expect(coldParts.length).toBe(1)
    expectToolCallPartContract(coldParts[0], {
      state: 'error',
      error: ERROR_TEXT,
      result: `Error: ${ERROR_TEXT}`,
      resultData: undefined,
    })
    expect(coldParts[0].state).toBe(hotPart.state)
    expect(coldParts[0].error).toBe(hotPart.error)
    expect(coldParts[0].result).toBe(hotPart.result)
  })

  it('冷路径 args：OpenAI function.arguments JSON 字符串被解析为对象', async () => {
    const coldParts = await buildColdParts([
      {
        ...coldAssistantMessage(),
        toolCalls: [
          {
            id: CALL_ID,
            type: 'function',
            function: { name: TOOL_NAME, arguments: '{"file_path":"src/main.rs","content":"fn main() {}\\n"}' },
          },
        ],
      },
      coldToolMessage(),
    ])
    expect(coldParts[0].args).toBeTypeOf('object')
    expect(coldParts[0].args).toEqual(ARGS)
  })

  it('冷路径 args：非法 JSON 字符串不抛异常（降级保留原值）', async () => {
    const coldParts = await buildColdParts([
      {
        ...coldAssistantMessage(),
        toolCalls: [
          { id: CALL_ID, type: 'function', function: { name: TOOL_NAME, arguments: '{"file_path": "unterminated' } },
        ],
      },
      coldToolMessage(),
    ])
    expect(coldParts[0].args).toBe('{"file_path": "unterminated')
  })
})
