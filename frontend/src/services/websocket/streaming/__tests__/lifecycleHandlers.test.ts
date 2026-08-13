/**
 * handleReconnected 测试 - WS 重连断线补漏行为
 *
 * 验证（修复目标）：
 * 1. 重连时对每个 streaming 管道执行 loadPipelineMessages(mode:'backfill') 补漏
 *    —— 为什么重要：断线期间后端通过 replay 缓冲累积的消息必须被前端拉回，
 *       否则子管道（useRealtimeEvents 只补主管道）的消息会静默丢失。
 * 2. 补漏成功 → 不弹「流式消息可能丢失」警告（消息已通过补漏恢复，无需打扰用户）
 * 3. 补漏失败 → 才弹「流式消息可能丢失」警告（补漏失败意味着消息确实可能丢失，需提示用户手动刷新）
 * 4. 无 streaming 管道 → 不补漏、不弹警告
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

// ── Mock 依赖 ──

const { mockStore, mockAddNotification } = vi.hoisted(() => ({
  mockStore: {
    streamingState: {},
    messagesByPipeline: {},
    pipelineSessionMap: {},
    updateMessage: vi.fn(),
    appendPart: vi.fn(),
    loadPipelineMessages: vi.fn(),
  },
  mockAddNotification: vi.fn(),
}))

vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: { getState: () => mockStore },
}))

vi.mock('@/stores/notificationStore', () => ({
  useNotificationStore: { getState: () => ({ addNotification: mockAddNotification }) },
}))

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
    websocket: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

vi.mock('../handlers/utils', () => ({
  terminatePipeline: vi.fn(),
  allocateNextSequence: vi.fn(),
}))

vi.mock('../router', () => ({
  resolvePipelineId: vi.fn(),
}))

import { handleReconnected } from '../lifecycleHandlers'

const STREAMING_PIPELINE = 'pipe-streaming-001'
const THREAD_ID = 'thread-001'

function setupStreamingPipeline(): void {
  mockStore.streamingState = {
    [STREAMING_PIPELINE]: { isStreaming: true, messageId: 'msg-001' },
  }
  mockStore.messagesByPipeline = {
    [STREAMING_PIPELINE]: [
      { id: 'msg-001', role: 'assistant', status: 'streaming', parts: [] },
    ],
  }
  mockStore.pipelineSessionMap = { [STREAMING_PIPELINE]: THREAD_ID }
  mockStore.updateMessage.mockClear()
  // 注意：不在此 mockReset loadPipelineMessages —— beforeEach 已设置默认
  // { ok: true }（补漏成功），mockReset 会清除该实现导致返回值 undefined、
  // 被误判为补漏失败。补漏失败场景由用例内显式 mockResolvedValue({ ok: false }) 覆盖。
  mockAddNotification.mockClear()
}

describe('handleReconnected - WS 重连断线补漏', () => {
  beforeEach(() => {
    mockStore.streamingState = {}
    mockStore.messagesByPipeline = {}
    mockStore.pipelineSessionMap = {}
    mockStore.loadPipelineMessages.mockReset()
    mockStore.loadPipelineMessages.mockResolvedValue({ ok: true })
    mockAddNotification.mockClear()
  })

  it('重连时对 streaming 管道执行 backfill 补漏', async () => {
    // Arrange
    setupStreamingPipeline()

    // Act
    await handleReconnected()

    // Assert：必须以该管道 + backfill 模式补漏，消息才能从后端拉回
    expect(mockStore.loadPipelineMessages).toHaveBeenCalledWith(
      STREAMING_PIPELINE,
      expect.objectContaining({
        threadId: THREAD_ID,
        mode: 'backfill',
        skipStreamingCheck: true,
      }),
    )
  })

  it('补漏成功时不弹「流式消息可能丢失」警告', async () => {
    // Arrange：loadPipelineMessages 默认返回 { ok: true }（补漏成功）
    setupStreamingPipeline()

    // Act
    await handleReconnected()

    // Assert：补漏成功说明消息已恢复，不应再提示「可能丢失」
    expect(mockAddNotification).not.toHaveBeenCalled()
  })

  it('补漏失败时弹「流式消息可能丢失」警告', async () => {
    // Arrange：补漏失败，消息确实可能丢失
    setupStreamingPipeline()
    mockStore.loadPipelineMessages.mockResolvedValue({ ok: false })

    // Act
    await handleReconnected()

    // Assert：只有补漏失败才提示用户手动刷新
    expect(mockAddNotification).toHaveBeenCalledWith(
      expect.objectContaining({ title: '流式消息可能丢失' }),
    )
  })

  it('无 streaming 管道时不调用补漏也不弹警告', async () => {
    // Act
    await handleReconnected()

    // Assert：没有正在流式的管道，无需补漏、无需警告
    expect(mockStore.loadPipelineMessages).not.toHaveBeenCalled()
    expect(mockAddNotification).not.toHaveBeenCalled()
  })
})
