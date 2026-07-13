/**
 * RAF 行为探测测试（非业务测试，用于确立 E2E 测试的环境基线）
 *
 * 目的：确认 vitest + jsdom 环境下 requestAnimationFrame 的真实行为。
 * 这决定了流式 chunk 的 RAF 批处理（streamHandler._flushChunks）在测试里
 * 到底会不会被逐帧触发，从而决定"转圈后一次弹出"能否在测试里复现。
 *
 * 预期结论之一：
 *  (A) jsdom 有 RAF polyfill 且能逐帧触发 → buffer 不会积压 → 需另寻根因
 *  (B) jsdom 的 RAF 是 setTimeout(0) 退化 → 会积压 → 能复现一次弹出
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

describe('jsdom requestAnimationFrame 行为探测', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('requestAnimationFrame 在 jsdom 中是否被定义', () => {
    console.log('[PROBE] typeof requestAnimationFrame =', typeof requestAnimationFrame)
    console.log('[PROBE] typeof cancelAnimationFrame =', typeof cancelAnimationFrame)
    expect(typeof requestAnimationFrame).not.toBe('undefined')
  })

  it('RAF 回调能否被触发（同步断言阶段）', () => {
    let called = false
    requestAnimationFrame(() => {
      called = true
    })
    // 同步阶段：RAF 不应已执行
    console.log('[PROBE] 同步阶段 RAF 是否已触发:', called)
    expect(called).toBe(false)
  })

  it('fake timer 下 RAF 回调的触发时机', async () => {
    vi.useFakeTimers()
    const timestamps: number[] = []
    const start = Date.now()
    requestAnimationFrame(() => {
      timestamps.push(Date.now() - start)
    })
    // 推进 16ms（一帧）与 50ms，看 RAF 在哪个时间点触发
    await vi.advanceTimersByTimeAsync(16)
    console.log('[PROBE] +16ms 后 RAF 触发次数:', timestamps.length, '时间:', timestamps)
    await vi.advanceTimersByTimeAsync(34)
    console.log('[PROBE] +50ms 后 RAF 触发次数:', timestamps.length, '时间:', timestamps)
  })

  it('多个 RAF 调度是否合并为单帧（批处理可行性）', async () => {
    vi.useFakeTimers()
    let flushCount = 0
    let rafId: number | null = null
    // 模拟 streamHandler._scheduleFlush 的幂等模式：已有挂起则不重复调度
    const scheduleFlush = () => {
      if (rafId === null) {
        rafId = requestAnimationFrame(() => {
          rafId = null
          flushCount++
        })
      }
    }
    // 模拟 10 个 chunk 到达，按幂等只调度一次 RAF
    for (let i = 0; i < 10; i++) scheduleFlush()
    await vi.advanceTimersByTimeAsync(16)
    console.log('[PROBE] 10 次幂等调度后 flush 次数（应=1）:', flushCount)
    expect(flushCount).toBe(1)
  })
})
