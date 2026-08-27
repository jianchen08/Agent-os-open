/** @feature 统一错误模型 | @ci: frontend-test */
/**
 * handlePluginError 插件执行错误通知（2026-08-27）
 *
 * plugin_error 事件（非终止信号）：引擎 warn+继续的插件失败（result.error /
 * invoker Err）经 WS 送达——消息本身正常收尾（new_message/stream_end 照常），
 * 此处只弹通知中心（errorSource=plugin），不标记消息失败、不终止管道。
 * 统一错误信封（config/error_codes.json）：code 缺省 PLUGIN_EXEC_FAILED。
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

const PIPELINE_ID = 'pipe-plugin-error-001'
const MESSAGE_ID = 'msg_plugin_error_01'
const THREAD_ID = 'thread-plugin-error-001'

function makePluginErrorEvent(error: unknown, pluginId?: string) {
  return {
    type: 'plugin_error',
    data: {
      pipeline_id: PIPELINE_ID,
      message_id: MESSAGE_ID,
      _threadId: THREAD_ID,
      plugin_id: pluginId,
      error,
    },
  }
}

describe('handlePluginError 插件执行错误通知（2026-08-27）', () => {
  let store: any
  let handlePluginError: any
  let wsWarn: ReturnType<typeof vi.fn>

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
            parts: [{ type: 'text', content: 'partial', state: 'streaming' }],
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
    handlePluginError = mod.handlePluginError
    const loggerMod = await import('@/utils/logger')
    wsWarn = loggerMod.loggers.websocket.warn as ReturnType<typeof vi.fn>
    wsWarn.mockClear()
    addNotificationMock.mockClear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('信封对象：弹通知（errorSource=plugin、normal 优先级），不标记消息失败', () => {
    handlePluginError(
      makePluginErrorEvent(
        {
          code: 'PLUGIN_EXEC_FAILED',
          message: '插件执行失败：sidecar 不可达',
          source: 'plugin',
          retryable: false,
        },
        'spill_guard',
      ),
    )
    // 消息保持 streaming（非终止信号：new_message/stream_end 照常收尾）
    const msg = store.getState().messagesByPipeline[PIPELINE_ID][0]
    expect(msg.status).toBe('streaming')
    expect(msg.error).toBeUndefined()
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.title).toBe('插件执行失败')
    expect(n.message).toContain('spill_guard')
    expect(n.message).toContain('sidecar 不可达')
    expect(n.priority).toBe('normal')
    expect(n.errorSource).toBe('plugin')
  })

  it('带 code 的错误信封：code 透传（通知定位用）', () => {
    handlePluginError(
      makePluginErrorEvent(
        {
          code: 'PLUGIN_CRASHED',
          message: '插件进程崩溃',
          source: 'plugin',
          retryable: false,
        },
        'llm_core',
      ),
    )
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toContain('llm_core')
    expect(n.message).toContain('插件进程崩溃')
  })

  it('error 缺失：兜底文案 + 缺省 code，不炸', () => {
    handlePluginError({ type: 'plugin_error', data: { pipeline_id: PIPELINE_ID } })
    const n = addNotificationMock.mock.calls[0][0]
    expect(n.message).toContain('插件执行失败')
    expect(n.errorSource).toBe('plugin')
  })

  it('pipeline_id 缺失：跳过通知（不拿 threadId 顶替）', () => {
    handlePluginError({ type: 'plugin_error', data: { _threadId: THREAD_ID } })
    expect(addNotificationMock).not.toHaveBeenCalled()
  })
})
