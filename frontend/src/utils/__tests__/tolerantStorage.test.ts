/**
 * createTolerantStorage 回归测试
 *
 * Bug 场景：localStorage 配额满时，zustand persist 的默认 storage 抛
 * QuotaExceededError 并冒泡到 store action（如 toggleMode），导致控制台
 * Uncaught 且内存状态也未更新。
 *
 * 期望：使用 createTolerantStorage 后，setItem 失败被吞掉，action 不抛异常，
 *      内存 state 正常更新。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

// 静音 logger，避免 warn/error 刷测试输出（沿用 persistQuotaExceeded.test.ts 写法）
vi.mock('@/utils/logger', () => ({
  loggers: {
    storage: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

const { createTolerantStorage } = await import('@/utils/tolerantStorage')

describe('createTolerantStorage', () => {
  let originalSetItem: typeof Storage.prototype.setItem

  beforeEach(() => {
    originalSetItem = Storage.prototype.setItem
    localStorage.clear()
  })

  afterEach(() => {
    Storage.prototype.setItem = originalSetItem
    localStorage.clear()
    vi.resetModules()
  })

  const makeStore = () =>
    create<{ mode: 'a' | 'b'; toggle: () => void }>()(
      persist(
        (set) => ({
          mode: 'a',
          toggle: () => set((s) => ({ mode: s.mode === 'a' ? 'b' : 'a' })),
        }),
        {
          name: 'tolerant-test',
          storage: createTolerantStorage(),
        },
      ),
    )

  it('配额满时 setItem 抛 QuotaExceededError，action 不应抛异常且内存 state 应更新', () => {
    // 模拟 localStorage 配额已满：任何 setItem 都抛 QuotaExceededError
    Storage.prototype.setItem = vi.fn(() => {
      throw new DOMException(
        "Failed to execute 'setItem' on 'Storage': Setting the value exceeded the quota.",
        'QuotaExceededError',
      )
    })

    const useStore = makeStore()

    // toggle 触发 persist 写入：不应抛出
    expect(() => useStore.getState().toggle()).not.toThrow()

    // 内存 state 必须更新成功（persist 失败不影响业务）
    expect(useStore.getState().mode).toBe('b')
  })

  it('连续多次写入（每次触发 persist）在配额满时都应成功', () => {
    Storage.prototype.setItem = vi.fn(() => {
      throw new DOMException('quota exceeded', 'QuotaExceededError')
    })

    const useStore = makeStore()
    const store = useStore.getState()

    expect(() => store.toggle()).not.toThrow()
    expect(() => store.toggle()).not.toThrow()

    expect(useStore.getState().mode).toBe('a')
  })

  it('正常情况下应正常读写（未触发配额错误）', () => {
    const useStore = makeStore()
    useStore.getState().toggle()

    expect(useStore.getState().mode).toBe('b')
    // persist 的 trailing 写入在 jsdom 下同步落盘
    expect(localStorage.getItem('tolerant-test')).toContain('"b"')
  })
})
