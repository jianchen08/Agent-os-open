/**
 * Bug 2 回归测试：发送消息后应立即出现"思考中"占位气泡
 *
 * 修复要点：router.handleSendMessage 把 ensureStreamingPlaceholder 调用
 * 提前到 globalWS.sendUserInput 之前同步执行，用户点发送的瞬间就出现
 * 占位气泡（而非等 stream_start / send 成功之后）。
 *
 * 由于 sendUserInput 是同步入队（_send 永不抛异常），不存在"发送失败需回滚"
 * 路径——占位气泡可以在 send 之前无条件创建。
 *
 * 本测试验证修复依赖的两个真实不变量（直接驱动生产函数 ensureStreamingPlaceholder，
 * 而非自证式模拟）：
 *   1. 发送阶段：ensureStreamingPlaceholder(placeholder_X) 立即创建 streaming assistant
 *      占位消息，前一条是 user 时新建独立气泡。
 *   2. 衔接阶段：stream_start 到达时 ensureStreamingPlaceholder(realMsgId) 发现前一条
 *      是 assistant（占位气泡），走合并分支把 placeholder id 改写为真实 messageId，
 *      后续 chunk 按 realMsgId 落到同一条消息。
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
const PLACEHOLDER_ID = 'placeholder_test-uuid-001'
const REAL_MESSAGE_ID = 'msg-real-from-backend-001'

describe('Bug 2: 发送消息瞬间应立即出现"思考中"占位气泡', () => {
  let usePipelineMessageStore: typeof import('@/stores/pipelineMessageStore').usePipelineMessageStore
  let ensureStreamingPlaceholder: typeof import('@/services/websocket/streaming/handlers/utils').ensureStreamingPlaceholder

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

    const utilsMod = await import('@/services/websocket/streaming/handlers/utils')
    ensureStreamingPlaceholder = utilsMod.ensureStreamingPlaceholder
  })

  /** 模拟发送阶段：用户消息已添加，占位气泡在 send 之前同步创建 */
  function simulateSendPhase() {
    const store = usePipelineMessageStore.getState()
    const userSeq = store.getMessages(PIPELINE_ID).reduce(
      (max, m) => Math.max(max, m.sequence ?? 0), 0,
    ) + 1

    // 1. 用户消息入列（router.handleSendMessage 中 addMessage）
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

    // 2. 发送前同步创建占位气泡（修复后的时序，不依赖 sendUserInput 结果）
    ensureStreamingPlaceholder(PIPELINE_ID, PLACEHOLDER_ID, SESSION_ID)
  }

  it('发送阶段：ensureStreamingPlaceholder 应立即创建 streaming assistant 占位消息', () => {
    simulateSendPhase()

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    // 用户消息 + 1 个 assistant 占位
    expect(msgs).toHaveLength(2)

    const placeholder = msgs[1]
    expect(placeholder.role).toBe('assistant')
    expect(placeholder.status).toBe('streaming')
    expect(placeholder.id).toBe(PLACEHOLDER_ID)
  })

  it('发送阶段：占位气泡应启动管道 streaming 状态（输入框进入生成态）', () => {
    simulateSendPhase()

    const streamState = usePipelineMessageStore.getState().streamingState[PIPELINE_ID]
    expect(streamState).toBeDefined()
    expect(streamState.isStreaming).toBe(true)
  })

  it('衔接阶段：stream_start 到达时应把占位气泡 id 改写为后端真实 messageId', () => {
    simulateSendPhase()

    // stream_start 携带后端真实 messageId，再次调用 ensureStreamingPlaceholder
    // 前一条已是 assistant（占位气泡）→ 走合并分支，updateMessage 改写 id
    ensureStreamingPlaceholder(PIPELINE_ID, REAL_MESSAGE_ID, SESSION_ID)

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    // 不应新增气泡（合并而非新建）
    expect(msgs).toHaveLength(2)

    const assistantMsg = msgs[1]
    // id 已被改写为真实 messageId，后续 chunk 按 realMsgId 落到这条消息
    expect(assistantMsg.id).toBe(REAL_MESSAGE_ID)
    expect(assistantMsg.role).toBe('assistant')
    expect(assistantMsg.status).toBe('streaming')
    // placeholder_ 前缀的旧 id 应彻底消失
    expect(msgs.find((m) => m.id === PLACEHOLDER_ID)).toBeUndefined()
  })

  it('衔接阶段：id 改写后，按真实 messageId 追加 part 能落到同一条消息', () => {
    simulateSendPhase()
    ensureStreamingPlaceholder(PIPELINE_ID, REAL_MESSAGE_ID, SESSION_ID)

    const store = usePipelineMessageStore.getState()
    // 模拟 stream_chunk 的 appendPart（按 realMsgId 操作）
    store.appendPart(PIPELINE_ID, REAL_MESSAGE_ID, {
      type: 'text',
      content: 'AI 回复内容',
      state: 'streaming',
    } as any)

    const msgs = store.getMessages(PIPELINE_ID)
    const assistantMsg = msgs[1]
    expect(assistantMsg.id).toBe(REAL_MESSAGE_ID)
    expect(assistantMsg.parts).toHaveLength(1)
    expect((assistantMsg.parts as any[])[0].content).toBe('AI 回复内容')
  })

  /**
   * 回归测试：断线重连后空气泡根因修复。
   * 病灶：ensureStreamingPlaceholder 只看 role==='assistant' 就改写上一条 id，
   * 刷新后上一条是已 completed 的 API 权威消息（hex id），chunk 来了把它改写
   * 成新 messageId → 后续对账全乱 → 空气泡。
   * 修复：id 改写条件收紧为 status==='streaming'，completed 消息的 id 冻结。
   */
  it('回归：前一条是 completed assistant 时绝不改写其 id（防空气泡）', () => {
    const store = usePipelineMessageStore.getState()
    const COMPLETED_HEX_ID = 'msg-completed-aabbcc'
    // 构造"刷新后"场景：上一条是已 completed 的 API 权威消息（hex id）
    store.addMessage(PIPELINE_ID, {
      id: COMPLETED_HEX_ID,
      sessionId: SESSION_ID,
      role: 'user',
      content: '问题',
      sequence: 1,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'completed',
    } as Message)
    store.addMessage(PIPELINE_ID, {
      id: COMPLETED_HEX_ID,
      sessionId: SESSION_ID,
      role: 'assistant',
      content: '已完成的后端权威回复',
      sequence: 2,
      timestamp: new Date().toISOString(),
      parentId: null,
      status: 'completed', // 关键：已完成，id 必须冻结
    } as Message)

    // 断线期间 stream_start 丢失，迟到的 thinking_chunk 触发 auto-create
    // 传入新的 hex messageId（后端本轮新 turn 的）
    const LATE_MSG_ID = 'msg-late-turn2-1122'
    ensureStreamingPlaceholder(PIPELINE_ID, LATE_MSG_ID, SESSION_ID)

    const msgs = store.getMessages(PIPELINE_ID)
    // 已完成的权威消息 id 不应被改写（仍存在且 id 不变）
    const completedMsg = msgs.find((m) => m.content === '已完成的后端权威回复')
    expect(completedMsg).toBeDefined()
    expect(completedMsg!.id).toBe(COMPLETED_HEX_ID)
    expect(completedMsg!.status).toBe('completed')
    // 应新建独立气泡（而非合并到已完成消息）
    const newBubble = msgs.find((m) => m.id === LATE_MSG_ID)
    expect(newBubble).toBeDefined()
    expect(newBubble!.status).toBe('streaming')
  })
})
