/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 点停即时反馈（2026-11-07）：停止护栏 TTL 与「停止受理中」（stoppingByPipeline）
 * 的自动翻转。
 *
 * 契约：
 * - 点停（markStopRequested + setStopping(true)）后护栏 TTL 过期仍无终态事件
 *   （pipeline_round_finished / run_failed / stream_error）→ stopping 自动解除：
 *   生成观感回落 runs 真值（run 仍在跑 → 转圈恢复，停止未生效可辨别、可再点），
 *   不静默装作已停。
 * - 终态事件先到 → 护栏收口（clearStopRequested）撤销定时器，无迟到翻转副作用。
 * - 重按停止 → TTL 窗口重置。
 * - 新一轮 stream_start（不同消息 id）→ 护栏解除 + 定时器撤销。
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

const PID = 'pipe-stop-feedback-001'
const MSG_ID = 'msg_stop_feedback_01'
const MSG_ID_R2 = 'msg_stop_feedback_02'
const TID = 'thread-stop-feedback-001'

function makeEvent(type: string, data: Record<string, any> = {}) {
  return { type, data: { pipeline_id: PID, _threadId: TID, ...data } }
}

describe('点停即时反馈：停止意图粘性（2026-11-19 裁定——TTL 到期不复位生成观感）', () => {
  let store: any
  let markStopRequested: any
  let handlePipelineRoundFinished: any
  let handleStreamStart: any

  beforeEach(async () => {
    vi.useFakeTimers()
    store = await resetStreamingStore(PID, TID, PID)
    const utils = await import('@/services/websocket/streaming/handlers/utils')
    const mod = await import('@/services/websocket/streaming/handlers')
    markStopRequested = utils.markStopRequested
    handlePipelineRoundFinished = mod.handlePipelineRoundFinished
    handleStreamStart = mod.handleStreamStart
    applyStreamStatusMock.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (window as any).__pipelineStore
  })

  const stopping = () => !!store.getState().stoppingByPipeline[PID]

  it('护栏过期仍无终态事件 → stopping 保持粘性不复位（TTL 到期只触发对账，防「点停后又变回执行中」回跳）', async () => {
    markStopRequested(PID, MSG_ID)
    store.getState().setStopping(PID, true)
    expect(stopping()).toBe(true)

    await vi.advanceTimersByTimeAsync(15_000 - 1)
    expect(stopping()).toBe(true) // TTL 未到：停止窗口保持

    await vi.advanceTimersByTimeAsync(1)
    // 2026-11-19 用户裁定（停止意图粘性）：TTL 到期不复位 stopping——复位会在
    // runStatus 仍是旧值 running（30s 兜底轮询）时回跳「执行中」。清除只由终态
    // 收口（run cancelled/completed/failed）承担，或用户新一轮 stream_start。
    expect(stopping()).toBe(true)
  })

  it('终态事件先到（round_finished cancelled）→ stopping 落定 + 注册表标 cancelled；定时器撤销，推进 15s 无迟到翻转', async () => {
    markStopRequested(PID, MSG_ID)
    store.getState().setStopping(PID, true)

    handlePipelineRoundFinished(makeEvent('pipeline_round_finished', { cancelled: true }))
    expect(stopping()).toBe(false)
    expect(applyStreamStatusMock).toHaveBeenCalledWith(PID, 'cancelled')

    await vi.advanceTimersByTimeAsync(15_000)
    expect(stopping()).toBe(false) // 定时器已随终态撤销，无迟到副作用
  })

  it('重按停止重置护栏窗口（stopping 粘性保持，重按只续 isStopRequested 压制窗）', async () => {
    markStopRequested(PID, MSG_ID)
    store.getState().setStopping(PID, true)

    await vi.advanceTimersByTimeAsync(10_000)
    markStopRequested(PID, MSG_ID) // 用户再点一次停止

    await vi.advanceTimersByTimeAsync(5_000) // 距首次 mark 已 15s
    expect(stopping()).toBe(true) // 粘性：不翻转

    await vi.advanceTimersByTimeAsync(10_000) // 距重按 15s
    expect(stopping()).toBe(true) // 仍粘性：TTL 到期不复位（2026-11-19 裁定）
  })

  it('新一轮 stream_start（不同消息 id）→ 护栏解除 + 定时器撤销，推进 15s 无翻转副作用', async () => {
    markStopRequested(PID, MSG_ID)
    store.getState().setStopping(PID, true)

    handleStreamStart(makeEvent('stream_start', { message_id: MSG_ID_R2 }))
    expect(stopping()).toBe(false) // stream_start 自身清 stopping（既有契约）

    await vi.advanceTimersByTimeAsync(15_000)
    expect(stopping()).toBe(false)
  })
})
