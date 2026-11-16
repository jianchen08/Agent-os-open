/** @feature: FP-0.2.流式气泡稳定 入口占位行权威排序坐标 | @ci: frontend-test */
/**
 * stream_start 携带消息 sequence（入口占位行，ADR
 * 2026-11-06-round-entry-slot-placeholder 决策 2）：handleStreamStart 把事件
 * payload 内层的 sequence 传入 ensureStreamingPlaceholder 既有 backendSequence
 * 参数——占位气泡从第一毫秒起有权威排序坐标，不再挂空垫底。
 *
 * 三条契约：
 * 1. stream_start data.sequence 在场 → 占位气泡 sequence = 该权威值。
 * 2. stream_start 无 sequence（旧内核/降级世界）→ 占位 seq 挂空（既有回落，
 *    不劣化）。
 * 3. 事件信封顶层 sequence 是全局事件计数器（非消息 seq）——不得被当消息序
 *    消费（严禁混排，用户裁定 2026-11-06：无实体不渲染、不做排序兜底）。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const applyStreamStatusMock = vi.fn()
vi.mock('@/stores/pipelineRegistryStore', () => ({
  usePipelineRegistryStore: {
    getState: () => ({ applyStreamStatus: applyStreamStatusMock }),
  },
}))

const PIPELINE_ID = 'pipe-seq-001'
const THREAD_ID = 'thread-seq-001'

/** 信封形态（GlobalWebSocket 透传整封）：顶层 sequence = 事件总线计数器，
 * data 内层才是事件 payload。 */
function streamStartEnvelope(data: Record<string, any>) {
  return {
    type: 'stream_start',
    sequence: 99999,
    data: {
      pipeline_id: PIPELINE_ID,
      _threadId: THREAD_ID,
      ...data,
    },
  }
}

describe('stream_start.sequence 落位占位气泡（入口占位行）', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any

  beforeEach(async () => {
    vi.useFakeTimers()
    usePipelineMessageStore = await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)
    const mod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = mod.handleStreamStart
    applyStreamStatusMock.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (window as any).__pipelineStore
  })

  function placeholderOf(messageId: string) {
    return usePipelineMessageStore
      .getState()
      .getMessages(PIPELINE_ID)
      .find((m: any) => m.id === messageId)
  }

  it('data.sequence 在场 → 占位气泡获得权威 seq', () => {
    handleStreamStart(
      streamStartEnvelope({ message_id: 'a_seq_full', sequence: 7 }),
    )
    const msg = placeholderOf('a_seq_full')
    expect(msg).toBeDefined()
    expect(msg.sequence).toBe(7)
    expect(msg.status).toBe('streaming')
  })

  it('无 sequence（旧内核回落）→ 占位 seq 挂空', () => {
    handleStreamStart(streamStartEnvelope({ message_id: 'a_seq_missing' }))
    const msg = placeholderOf('a_seq_missing')
    expect(msg).toBeDefined()
    expect(msg.sequence).toBeUndefined()
  })

  it('信封顶层 sequence（事件计数器）不得当消息序消费', () => {
    // 顶层 99999 是事件总线计数器；payload 不带消息 seq → 挂空，绝不取顶层值。
    handleStreamStart(streamStartEnvelope({ message_id: 'a_seq_guard' }))
    const msg = placeholderOf('a_seq_guard')
    expect(msg).toBeDefined()
    expect(msg.sequence).toBeUndefined()
    expect(msg.sequence).not.toBe(99999)
  })
})
