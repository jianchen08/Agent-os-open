/** @feature FP-0.2.二 前端持久化内存修复 | @ci: frontend-test */
/**
 * createPersistThrottle 多名写入测试
 *
 * 旧实现单槽缓冲（buffer 只存一份 name/value）：同一窗口内第二个 name 的
 * schedule 会静默覆盖第一个 name 的挂起写入 → 首个 store 的落盘丢失
 * （直到它下次写入才补上，maxWait 期间可能一直丢）。多名共享一个节流器
 * 实例（indexedDbStorage 模块级单例）时此丢写真实可触发。
 *
 * 契约：缓冲按 name 分槽——不同 name 互不覆盖、flush 时各自落盘；
 * 同名多次写入仍只保留最后一次（trailing 合并语义不变）。
 */
import { describe, it, expect, vi } from 'vitest'
import { createPersistThrottle } from '@/utils/persistThrottle'

describe('createPersistThrottle 多名写入', () => {
  it('同一窗口内不同 name 互不覆盖，flush 时各自落盘', () => {
    vi.useFakeTimers()
    try {
      const writes: Array<[string, string]> = []
      const throttle = createPersistThrottle((name, value) => {
        writes.push([name, value])
      })

      throttle.schedule('store-a', 'a1')
      throttle.schedule('store-b', 'b1')
      vi.advanceTimersByTime(1100)

      expect(writes).toContainEqual(['store-a', 'a1'])
      expect(writes).toContainEqual(['store-b', 'b1'])
    } finally {
      vi.useRealTimers()
    }
  })

  it('窗口内同名多次写入仍只保留最后一次（trailing 合并不回归）', () => {
    vi.useFakeTimers()
    try {
      const writes: Array<[string, string]> = []
      const throttle = createPersistThrottle((name, value) => {
        writes.push([name, value])
      })

      throttle.schedule('store-a', 'a1')
      throttle.schedule('store-a', 'a2')
      vi.advanceTimersByTime(1100)

      expect(writes).toEqual([['store-a', 'a2']])
    } finally {
      vi.useRealTimers()
    }
  })

  it('cancel 清空所有 name 的挂起写入', () => {
    vi.useFakeTimers()
    try {
      const writes: Array<[string, string]> = []
      const throttle = createPersistThrottle((name, value) => {
        writes.push([name, value])
      })

      throttle.schedule('store-a', 'a1')
      throttle.schedule('store-b', 'b1')
      throttle.cancel()
      vi.advanceTimersByTime(1100)

      expect(writes).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })
})
