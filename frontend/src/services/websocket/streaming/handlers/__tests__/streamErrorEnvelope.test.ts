/** @feature 统一错误模型 | @ci: frontend-test */
/**
 * handleStreamError 统一错误信封解析（2026-08-26）
 *
 * stream_error 事件的 error 由后端升级为信封对象
 * （config/error_codes.json 单一真值源：{code, message, source, retryable}）：
 * - 消息落顶层 error 字段（source 渲染来源标签）
 * - 通知中心带 errorSource
 * - 旧形态字符串 error 兼容（不落 error 字段，文案照常展示）
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

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

vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: { getState: () => ({ clear: vi.fn(), set: vi.fn(), get: () => null }) },
}))

const { addNotificationMock } = vi.hoisted(() => ({
  addNotificationMock: vi.fn(),
}))

vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: addNotificationMock }) },
}))

const PIPELINE_ID = 'pipe-stream-error-env-001'
const MESSAGE_ID = 'msg_stream_error_env_01'
const THREAD_ID = 'thread-stream-error-env-001'

function makeStreamErrorEvent(error: unknown) {
  return {
    type: 'stream_error',
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      error,
    },
  }
}

describe('handleStreamError 统一错误信封（2026-08-26）', () => {
  let store: any
  let handleStreamError: any
  let wsError: ReturnType<typeof vi.fn>

  beforeEach(async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    store = storeMod.usePipelineMessageStore
    ;(window as any).__pipelineStore = store
    store.setState({
      messagesByPipeline: {
        [PIPELINE_ID]: [
          {
            id: MESSAGE_ID,
            role: 'assistant',
            status: 'streaming',
            content: 'partial',
            parts: [
              { type: 'text', content: 'partial', state: 'streaming' },
              {
                type: 'tool_call',
                callId: 'call_1',
                name: 'bash_execute',
                args: {},
                state: 'calling',
              },
            ],
            sequence: 1,
            timestamp: new Date().toISOString(),
          },
        ],
      },
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: { [PIPELINE_ID]: true },
      pipelines: {},
      activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
    })
    const mod = await import('@/services/websocket/streaming/handlers')
    handleStreamError = mod.handleStreamError
    const loggerMod = await import('@/utils/logger')
    wsError = loggerMod.loggers.websocket.error as ReturnType<typeof vi.fn>
    wsError.mockClear()
    addNotificationMock.mockClear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('信封对象：消息落 error 元数据（code/message/source/retryable）+ 通知带来源', () => {
    handleStreamError(
      makeStreamErrorEvent({
        code: 'ENGINE_RUN_FAILED',
        message: '引擎执行失败：llm provider 超时',
        source: 'kernel',
        retryable: true,
      }),
    )
    const msg = store.getState().messagesByPipeline[PIPELINE_ID][0]
    expect(msg.status).toBe('error')
    expect(msg.error).toEqual({
      code: 'ENGINE_RUN_FAILED',
      message: '引擎执行失败：llm provider 超时',
      source: 'kernel',
      retryable: true,
    })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toBe('引擎执行失败：llm provider 超时')
    expect(n.errorSource).toBe('kernel')
  })

  it('信封对象：streaming 的 text part 收尾为 done、calling 的 tool_call 标 error', () => {
    handleStreamError(
      makeStreamErrorEvent({
        code: 'ENGINE_RUN_FAILED',
        message: '引擎执行失败',
        source: 'kernel',
        retryable: true,
      }),
    )
    const parts = store.getState().messagesByPipeline[PIPELINE_ID][0].parts
    expect(parts[0].state).toBe('done')
    expect(parts[1].state).toBe('error')
  })

  it('旧形态字符串 error：透传文案展示，不落 error 字段（兼容不炸）', () => {
    handleStreamError(makeStreamErrorEvent('旧后端错误文本'))
    const msg = store.getState().messagesByPipeline[PIPELINE_ID][0]
    expect(msg.status).toBe('error')
    expect(msg.error).toBeUndefined()
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toBe('旧后端错误文本')
    expect(n.errorSource).toBeUndefined()
  })

  it('error 缺失：兜底文案 + 无来源标签', () => {
    handleStreamError({ type: 'stream_error', data: { pipeline_id: PIPELINE_ID } })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toBe('流式响应异常')
    expect(n.errorSource).toBeUndefined()
  })
})
