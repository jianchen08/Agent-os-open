/** @feature LLM 流式 8 事件协议前端适配 | @ci frontend-test */
/**
 * 思考块未闭合收尾测试（2026-08-26 协议迁移）
 *
 * 旧 thinking_start/chunk/end 三事件与 90s thinking 超时兜底随 thinkingHandler
 * 退役（方案 2026-08-26 定稿，不留兼容层）。新协议下：
 * - 思考块由 block_start(reasoning)/block_end 表达，无独立 thinking_end；
 * - 块未闭合（block_end 丢失/乱序）时，stream_end 收尾的 mergeStreamingParts
 *   把残留 streaming 态收敛为 done（streamTimingRepro 场景3 同语义）；
 * - 不写任何合成文案（思考超时提示写进真实消息会污染内容且随 IndexedDB 持久化）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-reasoning-unclosed-001'
const MESSAGE_ID = 'msg_reasoning_unclosed_01'
const THREAD_ID = 'thread-reasoning-unclosed-001'

const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

describe('思考块未闭合收尾', () => {
  let handleStreamStart: any
  let handleStreamEnd: any
  let handleBlockStart: any
  let handleReasoningDelta: any

  beforeEach(async () => {
    await resetStreamingStore(PIPELINE_ID, THREAD_ID)

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = handlerMod.handleStreamStart
    handleStreamEnd = handlerMod.handleStreamEnd
    handleBlockStart = handlerMod.handleBlockStart
    handleReasoningDelta = handlerMod.handleReasoningDelta
  })

  afterEach(() => {
    delete (window as any).__pipelineStore
  })

  it('reasoning 块未闭合（block_end 丢失）→ stream_end 收敛为 done 且不写合成文案', () => {
    handleStreamStart(makeEvent('stream_start', {}))

    // 思考块开始 + delta，【故意不发 block_end】
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '真实思考内容' }))

    const before = (window as any).__pipelineStore.getState().getMessages(PIPELINE_ID)
      .find((m: any) => m.id === MESSAGE_ID)
    const partBefore = (before?.parts || []).find((p: any) => p.type === 'thinking')
    expect(partBefore?.state).toBe('streaming')

    // stream_end 到达：mergeStreamingParts 兜底把残留 streaming 收敛为 done
    handleStreamEnd(makeEvent('stream_end', {
      full_content: '', final_sequence: 5,
      parts: [
        { type: 'thinking', content: '真实思考内容', state: 'done', sequence: 1 },
      ],
    }))

    const msgs = (window as any).__pipelineStore.getState().getMessages(PIPELINE_ID)
    const part = msgs.find((m: any) => m.id === MESSAGE_ID)?.parts?.[0] as any
    expect(part).toBeDefined()
    expect(part.state).toBe('done')
    // 不写合成文案（诚实状态，无"思考超时"污染）
    expect(part.content ?? '').not.toContain('⏱')
    expect(part.content ?? '').not.toContain('思考超时')
    expect(part.content ?? '').toBe('真实思考内容')
  })
})
