/**
 * tool_result 终态解析回归测试（FE4 兜底反模式修复）
 *
 * 旧行为：`success ?? data?.success ?? true`——缺 success 字段一律按成功渲染，
 * 后端契约漂移（只带 error 不带 success）时失败工具在 UI 呈现为成功。
 *
 * 新契约（resolveToolResultState）：
 * - 显式 success 服从 success（false → error）
 * - 缺 success 且 error 非空 → error（fail-closed）
 * - success/error 均缺 → done，但节流 warn 一条（契约漂移可观测）
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    stream: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    pipelineStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
  createLogger: () => ({ debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() }),
}))

vi.mock('@/services/api/session', () => ({
  getMessages: vi.fn().mockResolvedValue({ messages: [], total: 0, session_id: '' }),
  mergeConsecutiveAssistantMessages: (msgs: any[]) => msgs,
}))

vi.mock('@/utils/retry', () => ({
  retry: (fn: () => any) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

const PIPELINE_ID = 'pipe-tool-result-fe4-001'
const MESSAGE_ID = 'msg_tool_result_fe4_01'
const THREAD_ID = 'thread-tool-result-fe4-001'

function makeResultEvent(data: Record<string, any>) {
  return {
    type: 'tool_result',
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      ...data,
    },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

function makeStartEvent(data: Record<string, any>) {
  return {
    type: 'tool_start',
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      ...data,
    },
    source_type: 'system',
    source_id: PIPELINE_ID,
    timestamp: new Date().toISOString(),
  }
}

describe('handleToolResult 终态解析（FE4：缺 success 不再默认成功）', () => {
  let store: any
  let handleToolStart: any
  let handleToolResult: any
  let wsWarn: ReturnType<typeof vi.fn>
  let seq = 0

  beforeEach(async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    store = storeMod.usePipelineMessageStore
    ;(window as any).__pipelineStore = store
    store.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {}, activePipelineId: null,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
    })
    store.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: THREAD_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleToolStart = handlerMod.handleToolStart
    handleToolResult = handlerMod.handleToolResult

    const loggerMod = await import('@/utils/logger')
    wsWarn = loggerMod.loggers.websocket.warn as ReturnType<typeof vi.fn>
    wsWarn.mockClear()
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  /** 建一个 tool_call part 并注入结果载荷，返回该 part 的终态 */
  function runTool(resultData: Record<string, any>): any {
    seq += 1
    const callId = `call-${seq}`
    handleToolStart(makeStartEvent({ call_id: callId, tool_name: 'demo_tool' }))
    handleToolResult(makeResultEvent({ call_id: callId, tool_name: 'demo_tool', ...resultData }))
    const msgs = store.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
    return (msg?.parts ?? []).find((p: any) => p.type === 'tool_call' && p.callId === callId)
  }

  it('显式 success:false → error', () => {
    const part = runTool({ success: false, error: 'boom' })
    expect(part.state).toBe('error')
  })

  it('显式 success:true → done', () => {
    const part = runTool({ success: true })
    expect(part.state).toBe('done')
  })

  it('FE4：缺 success 但 error 非空 → error（不再默认成功）', () => {
    const part = runTool({ error: 'backend contract drift' })
    expect(part.state).toBe('error')
    expect(part.error).toBe('backend contract drift')
  })

  it('FE4：success/error 均缺 → done 且节流 warn 一条', () => {
    const first = runTool({ result: 'ok' })
    expect(first.state).toBe('done')
    expect(wsWarn).toHaveBeenCalledWith(
      expect.stringContaining('success/error 字段均缺失'),
      'demo_tool',
    )
    // 第二次同样缺字段：30s 节流窗口内不再刷屏
    wsWarn.mockClear()
    const second = runTool({ result: 'ok2' })
    expect(second.state).toBe('done')
    expect(wsWarn).not.toHaveBeenCalled()
  })
})
