/** @feature: FP-0.2.流式气泡稳定 流式占位跨轮连续性 | @ci: frontend-test */
/**
 * 空轮次占位与「思考中」气泡闪烁（2026-10-17 用户实证：思考中气泡等几秒
 * 消失一下又出来）。
 *
 * 引擎轮次模型：前处理体（init）等无产出轮照常开轮——stream_start 先到，
 * 前端建出「思考中...」占位；轮末只发 stream_end（无 new_message、无 parts）。
 * 修复前契约：空轮次 stream_end 一律静默移除占位 → 占位删掉、主 LLM 轮
 * stream_start 再建新占位，中间存在无气泡窗口 = 可见闪烁。
 *
 * 修复后契约：
 * - run 仍在流式（streamingState.isStreaming）→ 空轮次占位保留，「思考中」
 *   指示跨轮无缝延续；下一轮 stream_start 的 orphan 清理删旧建新（同一事件
 *   内换血，无消失窗口）。
 * - run 级终止（pipeline_round_finished / run_failed）→ 残留空占位清扫移除，
 *   终态不留思考气泡。
 * - 生成态已被掐（isStreaming=false，如事件乱序/终止先行）→ 就地移除（旧行为，
 *   防残留）。
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

const PIPELINE_ID = 'pipe-empty-round-001'
const THREAD_ID = 'thread-empty-round-001'
const INIT_MSG_ID = 'a_init_round_empty_01'
const MAIN_MSG_ID = 'a_main_round_llm_02'

function makeEvent(eventType: string, data: Record<string, any>) {
  return {
    type: eventType,
    data: {
      pipeline_id: PIPELINE_ID,
      _threadId: THREAD_ID,
      ...data,
    },
  }
}

/** 后端空轮次 stream_end 实际形态：无 parts、无 final_sequence（ws_session.rs 桥接） */
function emptyRoundStreamEnd(messageId: string) {
  return makeEvent('stream_end', { message_id: messageId })
}

describe('空轮次占位连续性：思考气泡跨轮不闪没', () => {
  let usePipelineMessageStore: any
  let handleStreamStart: any
  let handleStreamEnd: any
  let handlePipelineRoundFinished: any
  let handleRunFailed: any

  beforeEach(async () => {
    vi.useFakeTimers()
    usePipelineMessageStore = await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)
    const mod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = mod.handleStreamStart
    handleStreamEnd = mod.handleStreamEnd
    handlePipelineRoundFinished = mod.handlePipelineRoundFinished
    handleRunFailed = mod.handleRunFailed
    applyStreamStatusMock.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    delete (window as any).__pipelineStore
  })

  const assistantPlaceholders = () =>
    usePipelineMessageStore
      .getState()
      .getMessages(PIPELINE_ID)
      .filter((m: any) => m.role === 'assistant')

  it('空轮次 stream_end（run 仍在流式）：占位保留，思考指示不消失', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: INIT_MSG_ID }))
    expect(assistantPlaceholders()).toHaveLength(1)

    handleStreamEnd(emptyRoundStreamEnd(INIT_MSG_ID))

    const kept = assistantPlaceholders()
    expect(kept).toHaveLength(1)
    expect(kept[0].id).toBe(INIT_MSG_ID)
    expect(kept[0].status).toBe('streaming')
  })

  it('下一轮 stream_start 换血：旧空占位移除、新占位就位，同事件内无真空', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: INIT_MSG_ID }))
    handleStreamEnd(emptyRoundStreamEnd(INIT_MSG_ID))

    handleStreamStart(makeEvent('stream_start', { message_id: MAIN_MSG_ID }))

    const placeholders = assistantPlaceholders()
    expect(placeholders).toHaveLength(1)
    expect(placeholders[0].id).toBe(MAIN_MSG_ID)
    expect(placeholders[0].status).toBe('streaming')
  })

  it('run 终态（pipeline_round_finished）：残留空占位清扫，不留思考气泡', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: INIT_MSG_ID }))
    handleStreamEnd(emptyRoundStreamEnd(INIT_MSG_ID))
    expect(assistantPlaceholders()).toHaveLength(1)

    handlePipelineRoundFinished(makeEvent('pipeline_round_finished', { failed: false }))

    expect(assistantPlaceholders()).toHaveLength(0)
  })

  it('run 终态（run_failed）：残留空占位同样清扫', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: INIT_MSG_ID }))
    handleStreamEnd(emptyRoundStreamEnd(INIT_MSG_ID))
    expect(assistantPlaceholders()).toHaveLength(1)

    handleRunFailed({
      data: { pipeline_id: PIPELINE_ID, _threadId: THREAD_ID, run_id: 'run-1' },
    })

    expect(assistantPlaceholders()).toHaveLength(0)
  })

  it('生成态已被终止（isStreaming=false）的空轮次 stream_end：就地移除（防残留）', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: INIT_MSG_ID }))
    usePipelineMessageStore.setState({ streamingState: {} })

    handleStreamEnd(emptyRoundStreamEnd(INIT_MSG_ID))

    expect(assistantPlaceholders()).toHaveLength(0)
  })

  it('有产出轮 stream_end 不受影响：消息收尾 completed（回归锚）', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MAIN_MSG_ID }))
    usePipelineMessageStore.getState().appendPart(PIPELINE_ID, MAIN_MSG_ID, {
      type: 'text',
      content: '正文',
      state: 'streaming',
    } as any)

    handleStreamEnd(
      makeEvent('stream_end', { message_id: MAIN_MSG_ID, final_sequence: 7 }),
    )

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    const landed = msgs.find((m: any) => m.id === MAIN_MSG_ID)
    expect(landed).toBeDefined()
    expect(landed.status).toBe('completed')
    expect(landed.sequence).toBe(7)
  })

  it('run 终态清扫不误删有内容的流式消息（断流残留另有人管）', () => {
    handleStreamStart(makeEvent('stream_start', { message_id: MAIN_MSG_ID }))
    usePipelineMessageStore.getState().appendPart(PIPELINE_ID, MAIN_MSG_ID, {
      type: 'text',
      content: '已流出的半截正文',
      state: 'streaming',
    } as any)

    handlePipelineRoundFinished(makeEvent('pipeline_round_finished', { failed: false }))

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE_ID)
    expect(msgs.some((m: any) => m.id === MAIN_MSG_ID)).toBe(true)
  })
})
