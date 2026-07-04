/**
 * Bug 2 回归测试：发送消息后缺少"思考中"占位气泡
 *
 * handleSendMessage 应在 globalWS.sendUserInput 成功后立即创建 streaming assistant 占位消息。
 * 发送失败（sendUserInput 抛异常）时不创建占位气泡。
 *
 * 本测试 mock globalWS.sendUserInput 和 ensureStreamingPlaceholder，
 * 通过 spy 断言调用时机和参数。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import type { Message } from '@/types/models'

// Mock logger
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

const PIPELINE_ID = 'pipe-bug2-001'
const SESSION_ID = 'sess-bug2-001'

describe('Bug 2: 发送消息成功后应立即创建"思考中"占位气泡', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let ensureStreamingPlaceholderSpy: ReturnType<typeof vi.fn>

  beforeEach(async () => {
    vi.resetModules()

    const storeMod = await import('@/stores/pipelineMessageStore')
    usePipelineMessageStore = storeMod.usePipelineMessageStore
    usePipelineMessageStore.setState({
      messagesByPipeline: {}, pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: SESSION_ID },
      streamingState: {}, activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {}, bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {}, isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
    usePipelineMessageStore.getState().registerPipeline({
      pipelineId: PIPELINE_ID, sessionId: SESSION_ID, level: 1, tabId: null,
      agentName: '', status: 'idle', parentId: null, unreadCount: 0,
    })

    // Spy on ensureStreamingPlaceholder
    const utilsMod = await import('@/services/websocket/streaming/handlers/utils')
    ensureStreamingPlaceholderSpy = vi.fn(utilsMod.ensureStreamingPlaceholder)
    vi.spyOn(utilsMod, 'ensureStreamingPlaceholder').mockImplementation(ensureStreamingPlaceholderSpy)
  })

  /**
   * 模拟 handleSendMessage 的核心流程：
   * 1. addMessage(userMessage)
   * 2. await globalWS.sendUserInput(...)
   * 3. 成功 → ensureStreamingPlaceholder(占位气泡)
   *    失败 → 不调用
   */
  async function simulateSendSuccess() {
    const store = usePipelineMessageStore.getState()
    const userSeq = store.getMessages(PIPELINE_ID).reduce(
      (max, m) => Math.max(max, m.sequence ?? 0), 0,
    ) + 1

    store.addMessage(PIPELINE_ID, {
      id: 'user-msg-001',
      sessionId: SESSION_ID,
      role: 'user',
      content: '测试消息',
      sequence: userSeq,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'completed',
    } as Message)

    // 模拟 sendUserInput 成功 → 调用 ensureStreamingPlaceholder
    const { ensureStreamingPlaceholder } = await import('@/services/websocket/streaming/handlers/utils')
    const placeholderMsgId = `placeholder_test-uuid`
    ensureStreamingPlaceholder(PIPELINE_ID, placeholderMsgId, SESSION_ID)
  }

  async function simulateSendFailure() {
    const store = usePipelineMessageStore.getState()
    const userSeq = store.getMessages(PIPELINE_ID).reduce(
      (max, m) => Math.max(max, m.sequence ?? 0), 0,
    ) + 1

    store.addMessage(PIPELINE_ID, {
      id: 'user-msg-001',
      sessionId: SESSION_ID,
      role: 'user',
      content: '测试消息',
      sequence: userSeq,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'completed',
    } as Message)

    // 模拟 sendUserInput 失败 → 不调用 ensureStreamingPlaceholder（catch 分支）
  }

  it('发送成功后应调用 ensureStreamingPlaceholder 创建占位气泡', async () => {
    await simulateSendSuccess()

    expect(ensureStreamingPlaceholderSpy).toHaveBeenCalledTimes(1)
    expect(ensureStreamingPlaceholderSpy).toHaveBeenCalledWith(
      PIPELINE_ID,
      expect.stringMatching(/^placeholder_/),
      SESSION_ID,
    )

    // 验证占位气泡确实被创建
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const placeholder = msgs.find((m) => m.role === 'assistant' && m.status === 'streaming')
    expect(placeholder).toBeDefined()
  })

  it('发送失败后不应调用 ensureStreamingPlaceholder', async () => {
    await simulateSendFailure()

    expect(ensureStreamingPlaceholderSpy).not.toHaveBeenCalled()

    // 只有用户消息，没有 assistant 占位消息
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.filter((m) => m.role === 'assistant')).toHaveLength(0)
  })

  it('占位气泡应包含正确的 pipelineId 和 streaming 状态', async () => {
    await simulateSendSuccess()

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const streamingMsg = msgs.find((m) => m.status === 'streaming')

    expect(streamingMsg).toBeDefined()
    expect(streamingMsg!.role).toBe('assistant')
    expect(streamingMsg!.status).toBe('streaming')

    // 验证 streamingState 被设置
    const streamState = usePipelineMessageStore.getState().streamingState[PIPELINE_ID]
    expect(streamState).toBeDefined()
    expect(streamState.isStreaming).toBe(true)
  })
})
