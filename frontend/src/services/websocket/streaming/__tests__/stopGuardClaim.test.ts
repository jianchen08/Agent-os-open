/** @feature FP-0.2.WS停止清除 None | @ci: frontend-test */
/**
 * 停止护栏 + new_message 认领链路测试（2026-10-21 停止体验根修）。
 *
 * 断行为（输入→store 状态），不钉实现：
 * - 停止护栏：点停后迟到的流式事件不再重点亮生成态（Stop 按钮复活 =
 *   「点了没反应」体感根因）；新一轮 stream_start（不同 message_id）解除；
 *   run 收尾事件收口；TTL 过期失效。
 * - new_message 不再终止生成态：轮级事件不得清流式（生成态唯一终止信号 =
 *   run 级 pipeline_round_finished / stream_error）；同一事件携带 cmid 认领
 *   回传时乐观 user 气泡原地升级 completed。
 * - stream_error 收口：生成态终止、护栏清除；不产生/不改写 user 气泡
 *   （忙时入队本就无本地气泡，权威形态由对账收敛）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
// ── Mock 依赖（vi.mock 提升，先于底部真实 import 执行）──
const { mockAddNotification, mockApplyStreamStatus, mockNotify } = vi.hoisted(() => ({
  mockAddNotification: vi.fn(),
  mockApplyStreamStatus: vi.fn(),
  mockNotify: vi.fn(),
}))
vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))
vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: mockAddNotification }) },
}))
vi.mock('@/stores/pipelineRegistryStore', () => ({
  usePipelineRegistryStore: { getState: () => ({ applyStreamStatus: mockApplyStreamStatus }) },
}))
vi.mock('@/stores/contextUsageStore', () => ({
  useContextUsageStore: { getState: () => ({ updateUsage: vi.fn() }) },
}))
vi.mock('@/hooks/queries/usePipelineRunsQuery', () => ({
  invalidatePipelineStates: vi.fn(),
}))
vi.mock('../handlers/blockHandler', () => ({
  clearBlockStateForMessage: vi.fn(),
  flushBlockBuffers: vi.fn(),
}))
vi.mock('../router', () => ({
  resolvePipelineId: vi.fn(() => PIPELINE),
  isPipelineRelevant: vi.fn(() => true),
}))
vi.mock('@/lib/systemNotifier', () => ({
  notifyPipelineMessage: mockNotify,
  extractNotifyText: vi.fn(() => ''),
}))
vi.mock('@/services/api/session', () => ({
  mapBackendMessageToMessage: vi.fn((m: { id?: string; role?: string; content?: string }) => ({
    id: m.id ?? '',
    role: m.role ?? 'assistant',
    content: m.content ?? '',
    status: 'completed',
    timestamp: new Date().toISOString(),
  })),
}))
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { handleNewMessage } from '../handlers/messageHandler'
import { handleStreamError } from '../handlers/streamHandler'
import {
  clearStopRequested,
  ensureStreamingPlaceholder,
  markStopRequested,
  noteFreshStreamStart,
} from '../handlers/utils'

const PIPELINE = 'pipe-stop-guard'

function resetStore(): void {
  usePipelineMessageStore.setState({
    messagesByPipeline: { [PIPELINE]: [] },
    streamingState: {},
  })
}

beforeEach(() => {
  resetStore()
  mockAddNotification.mockClear()
  mockApplyStreamStatus.mockClear()
  mockNotify.mockClear()
})

describe('停止护栏（迟到 chunk 不重点亮生成态）', () => {
  it('markStopRequested 后同消息流式事件不重启 startStreaming', () => {
    ensureStreamingPlaceholder(PIPELINE, 'a_1', 'thread-1')
    expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBe(true)

    usePipelineMessageStore.getState().stopStreaming(PIPELINE)
    markStopRequested(PIPELINE, 'a_1')

    // 迟到 chunk 路径再次调用 ensureStreamingPlaceholder：占位消息保留（内容可追加），
    // 但生成态不得重新点亮。
    ensureStreamingPlaceholder(PIPELINE, 'a_1', 'thread-1')
    expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBeFalsy()
    expect(
      usePipelineMessageStore.getState().getMessages(PIPELINE).some((m) => m.id === 'a_1'),
    ).toBe(true)
  })

  it('新一轮 stream_start（不同 message_id）解除护栏', () => {
    markStopRequested(PIPELINE, 'a_old')
    noteFreshStreamStart(PIPELINE, 'a_new')

    ensureStreamingPlaceholder(PIPELINE, 'a_new', 'thread-1')
    expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBe(true)
  })

  it('同 message_id 的重复 stream_start 不解除护栏（停止轮次的迟到重放）', () => {
    markStopRequested(PIPELINE, 'a_1')
    noteFreshStreamStart(PIPELINE, 'a_1')

    ensureStreamingPlaceholder(PIPELINE, 'a_1', 'thread-1')
    expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBeFalsy()
  })

  it('clearStopRequested 收口后恢复放行', () => {
    markStopRequested(PIPELINE, 'a_1')
    clearStopRequested(PIPELINE)
    ensureStreamingPlaceholder(PIPELINE, 'a_1', 'thread-1')
    expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBe(true)
  })

  it('TTL 过期后护栏自动失效', () => {
    vi.useFakeTimers()
    try {
      markStopRequested(PIPELINE, 'a_1')
      vi.advanceTimersByTime(16_000)
      ensureStreamingPlaceholder(PIPELINE, 'a_1', 'thread-1')
      expect(usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming).toBe(true)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('new_message 轮级事件不清生成态 + cmid 认领升级', () => {
  it('handleNewMessage 保留流式态，cmid 命中把乐观 user 气泡原地升级 completed', () => {
    const ps = usePipelineMessageStore.getState()
    ps.addMessage(PIPELINE, {
      id: 'u_opt_1',
      sessionId: 'thread-1',
      role: 'user',
      content: '帮我查一下',
      timestamp: new Date().toISOString(),
      status: 'sending',
      clientMessageId: 'cm-1',
    })
    ps.startStreaming(PIPELINE, 'a_1')

    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_1',
        client_message_id: 'cm-1',
        user_message: { id: 'mc_auth1', content: '帮我查一下', sequence: 12 },
        message: { id: 'a_1', role: 'assistant', content: '查好了：……' },
      },
    })

    const state = usePipelineMessageStore.getState()
    // 轮级事件不清生成态：生成态唯一终止信号 = run 级事件（round_finished/stream_error）
    expect(state.streamingState[PIPELINE]?.isStreaming).toBe(true)
    const user = state.getMessages(PIPELINE).find((m) => m.clientMessageId === 'cm-1')
    expect(user?.status).toBe('completed')
    expect(user?.recordId).toBe('mc_auth1')
    expect(user?.id).toBe('u_opt_1')
  })
})

describe('stream_error 收口', () => {
  it('生成态终止、护栏清除，不产生/不改写 user 气泡', () => {
    const ps = usePipelineMessageStore.getState()
    ps.startStreaming(PIPELINE, 'a_err')
    markStopRequested(PIPELINE, 'a_err')

    handleStreamError({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_err',
        error: { code: 'ENGINE_RUN_FAILED', message: '引擎执行失败' },
      },
    })

    const state = usePipelineMessageStore.getState()
    expect(state.streamingState[PIPELINE]?.isStreaming).toBeFalsy()
    // 忙时入队无本地气泡（排队期唯一视图 = 队列条）：失败收尾不造 user 气泡
    expect(state.getMessages(PIPELINE).filter((m) => m.role === 'user')).toHaveLength(0)

    // 收口后同消息事件恢复放行（不残留压制）
    ensureStreamingPlaceholder(PIPELINE, 'a_err', 'thread-1')
    expect(
      usePipelineMessageStore.getState().streamingState[PIPELINE]?.isStreaming,
    ).toBe(true)
  })
})
