/** @feature FP-0.2.可观测性 WS 流式块状态空闲回收 @ci frontend-test */
/** @ci frontend-test */
/**
 * blockHandler 块状态空闲回收测试（数据面瘦身批次 1.4）：
 * 断线丢终止事件的残留 _blockStates 无界常驻——新流注册时清理空闲 >10min 的
 * 条目。时序用 vi.useFakeTimers + advanceTimersByTime（禁零延迟 mock）。
 *
 * 断言可观察行为（闭块去重依赖块状态）：状态在（活跃窗口内）重放的 block_start
 * 被闭块守卫拦下不重复建 part；状态被回收后同一重放会建出新 part（状态确已清理）。
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { makeEventFactory } from './helpers/streamingEventFactory'
import { resetStreamingStore } from './helpers/streamingStoreKit'

vi.mock('@/utils/logger', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).loggerMockFull())

vi.mock('@/services/api/session', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).apiSessionMockFull())

vi.mock('@/utils/retry', async () => (await import('../../../../../stores/__tests__/helpers/storeTestMocks')).retryMockBase())

const PIPELINE_ID = 'pipe-sweep-001'
const MSG_A = 'msg_sweep_a'
const MSG_B = 'msg_sweep_b'
const THREAD_ID = 'thread-sweep-001'

const makeEventA = makeEventFactory(PIPELINE_ID, MSG_A)
const makeEventB = makeEventFactory(PIPELINE_ID, MSG_B)

describe('blockHandler 块状态空闲回收', () => {
  let store: any
  let h: Record<string, any>

  beforeEach(async () => {
    vi.useFakeTimers()
    store = await resetStreamingStore(PIPELINE_ID, THREAD_ID, PIPELINE_ID)
    h = await import('@/services/websocket/streaming/handlers')
  })

  afterEach(() => {
    vi.useRealTimers()
    delete (window as any).__pipelineStore
  })

  function textPartCount(messageId: string): number {
    const msgs = store.getState().getMessages(PIPELINE_ID)
    const msg = msgs.find((m: any) => m.id === messageId)
    return (msg?.parts ?? []).filter((p: any) => p.type === 'text').length
  }

  it('活跃窗口内重放 block_start 被闭块守卫拦下（不重复建 part）；空闲超阈后新流注册触发回收，重放建出新 part', () => {
    // 消息 A：块 0 开→闭（状态在，闭块去重生效）
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    h.handleBlockEnd(makeEventA('block_end', { index: 0, block: { block_type: 'text' } }))
    expect(textPartCount(MSG_A)).toBe(1)

    // 活跃窗口内重放：闭块守卫拦下，不重复建 part
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    expect(textPartCount(MSG_A)).toBe(1)

    // 空闲 11 分钟（>10min 阈值）后新流（消息 B）注册 → sweep 清掉 A 的残留状态
    vi.advanceTimersByTime(11 * 60 * 1000)
    h.handleBlockStart(makeEventB('block_start', { index: 0, block_type: 'text' }))
    expect(textPartCount(MSG_B)).toBe(1)

    // A 的状态已被回收：同一重放建出新 part（对照面，证明回收确实发生）
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    expect(textPartCount(MSG_A)).toBe(2)
  })

  it('未超阈（10min 内）的新流注册不回收：A 的闭块守卫仍生效', () => {
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    h.handleBlockEnd(makeEventA('block_end', { index: 0, block: { block_type: 'text' } }))

    // 9 分钟后再开新流（消息 B）：未超阈，A 状态保留
    vi.advanceTimersByTime(9 * 60 * 1000)
    h.handleBlockStart(makeEventB('block_start', { index: 0, block_type: 'text' }))

    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    expect(textPartCount(MSG_A)).toBe(1)
  })

  it('每次 touch 刷新活跃时刻：长流内多次事件不误回收', () => {
    // A 流持续活动 15 分钟（每分钟开+闭一个新块），再过 1 分钟新流（B）注册——
    // 距 A 最后一次 touch 仅 1 分钟，不回收；若 touch 不刷新活跃时刻
    // （lastActivity 停在开场 0 时刻），16 分钟 > 10min 会被误回收
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    h.handleBlockEnd(makeEventA('block_end', { index: 0, block: { block_type: 'text' } }))
    for (let i = 0; i < 15; i++) {
      vi.advanceTimersByTime(60 * 1000)
      h.handleBlockStart(makeEventA('block_start', { index: i + 1, block_type: 'text' }))
      h.handleBlockEnd(makeEventA('block_end', { index: i + 1, block: { block_type: 'text' } }))
    }
    vi.advanceTimersByTime(60 * 1000)
    h.handleBlockStart(makeEventB('block_start', { index: 0, block_type: 'text' }))

    // A 状态仍在：重放已闭的块 0 被闭块守卫拦下，不重复建 part
    h.handleBlockStart(makeEventA('block_start', { index: 0, block_type: 'text' }))
    expect(textPartCount(MSG_A)).toBe(16)
  })
})
