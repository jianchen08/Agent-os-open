/**
 * 回归测试：reasoning_delta 走 RAF 批处理（LLM 流式 8 事件协议，方案 2026-08-26）
 *
 * 旧 thinking_chunk 语义迁移：思考增量由 reasoning_delta{index, text} 表达，
 * 思考块起止由 block_start/block_end 表达（thinking_start/chunk/end 三事件退役）。
 * reasoning_delta 与 text_delta 共用块索引缓冲 + RAF 批处理：
 *  - 未推进 RAF 时 delta 留在缓冲，不立即写 store（防每增量一次重渲染阻塞主线程）
 *  - block_end / finish 前 flush，保证末尾增量不丢
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-reasoning-raf-001'
const MESSAGE_ID = 'msg_reasoning_raf_01'
const THREAD_ID = 'thread-reasoning-raf-001'

const makeEvent = makeEventFactory(PIPELINE_ID, MESSAGE_ID)

function snapshotThinking() {
  const store = (window as any).__pipelineStore
  const msgs = store.getState().getMessages(PIPELINE_ID)
  const msg = msgs.find((m: any) => m.id === MESSAGE_ID)
  if (!msg) return { found: false, content: '', state: '' }
  const tp = (msg.parts || []).find((p: any) => p.type === 'thinking')
  return { found: true, content: tp?.content || '', state: tp?.state || '' }
}

describe('reasoning_delta RAF 批处理', () => {
  let handleStreamStart: any
  let handleReasoningDelta: any
  let handleBlockStart: any
  let handleBlockEnd: any

  beforeEach(async () => {
    vi.useFakeTimers()
    await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)

    const handlerMod = await import('@/services/websocket/streaming/handlers')
    handleStreamStart = handlerMod.handleStreamStart
    handleReasoningDelta = handlerMod.handleReasoningDelta
    handleBlockStart = handlerMod.handleBlockStart
    handleBlockEnd = handlerMod.handleBlockEnd

    handleStreamStart(makeEvent('stream_start', { sequence: 1 }))
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  it('场景1：reasoning_delta 逐帧累积（RAF 批处理生效）', async () => {
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))

    const chunks = ['让', '我', '想', '想']
    const seen: any[] = []
    for (let i = 0; i < chunks.length; i++) {
      handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: chunks[i] }))
      await vi.advanceTimersByTimeAsync(16)
      seen.push(snapshotThinking())
    }

    // 每个 delta 后推进一帧，thinking content 应逐帧累积
    expect(seen[0].content).toBe('让')
    expect(seen[1].content).toBe('让我')
    expect(seen[2].content).toBe('让我想')
    expect(seen[3].content).toBe('让我想想')
  })

  it('场景2：未推进 RAF 时 reasoning delta 在 buffer，不立即写入', () => {
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))

    // 连续发 delta 不推进 RAF
    for (const chunk of ['让', '我', '想', '想']) {
      handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: chunk }))
    }
    const beforeFlush = snapshotThinking()
    // delta 还在 buffer，thinking part 内容为空
    expect(beforeFlush.content).toBe('')

    // block_end 会 flush 缓冲
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning', text: '让我想想' } }))
    const afterEnd = snapshotThinking()
    // flush 后内容一次性出现（block_end 触发 flush）+ part 置 done
    expect(afterEnd.content).toBe('让我想想')
    expect(afterEnd.state).toBe('done')
  })

  it('场景3：block_end 前 flush 不丢末尾内容', async () => {
    handleBlockStart(makeEvent('block_start', { index: 0, block_type: 'reasoning' }))
    handleReasoningDelta(makeEvent('reasoning_delta', { index: 0, text: '思考内容' }))
    // 不推进 RAF，直接 block_end
    handleBlockEnd(makeEvent('block_end', { index: 0, block: { block_type: 'reasoning' } }))

    const snap = snapshotThinking()
    expect(snap.content).toBe('思考内容')
    expect(snap.state).toBe('done')
  })
})
