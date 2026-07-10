/**
 * indexedDbStorage 测试
 *
 * 验证：真实 IndexedDB 读写（fake-indexeddb 注入）、节流合并、容错降级（内存回退）。
 * 该 adapter 替代 localStorage 作为消息缓存存储，需保证 setItem/getItem/removeItem 语义正确。
 *
 * 注意：indexedDbStorage 是 createJSONStorage 返回的 PersistStorage，
 * 其 setItem/getItem/removeItem 即 zustand 适配器接口（name, value 两参）。
 * setItem 走节流（trailing 合并），需用 flushIndexedDbPersist 强制落盘或推进定时器后读取。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import fakeIndexedDB from 'fake-indexeddb'

// 注入 fake IndexedDB 到 globalThis，让 idb-keyval 在 jsdom 下走真实 IndexedDB 路径
;(globalThis as any).indexedDB = fakeIndexedDB

describe('indexedDbStorage', () => {
  let indexedDbStorage: NonNullable<ReturnType<typeof import('@/utils/indexedDbStorage')['indexedDbStorage']>>
  let flushIndexedDbPersist: typeof import('@/utils/indexedDbStorage')['flushIndexedDbPersist']

  beforeEach(async () => {
    vi.resetModules()
    const mod = await import('@/utils/indexedDbStorage')
    indexedDbStorage = mod.indexedDbStorage!
    flushIndexedDbPersist = mod.flushIndexedDbPersist
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('setItem 后 getItem 应返回相同值（基础读写）', async () => {
    indexedDbStorage.setItem('test-key', '{"a":1}')
    // setItem 经节流，强制落盘
    flushIndexedDbPersist()
    // flush 内部 void write(...)（异步 safeSet），等微任务 + IndexedDB 写入完成
    await vi.waitFor(async () => {
      const val = await indexedDbStorage.getItem('test-key')
      expect(val).toBe('{"a":1}')
    })
  })

  it('getItem 读不存在的 key 应返回 null', async () => {
    const val = await indexedDbStorage.getItem('non-existent-key-xyz')
    expect(val).toBeNull()
  })

  it('removeItem 后再读应返回 null', async () => {
    indexedDbStorage.setItem('to-remove', 'data')
    flushIndexedDbPersist()
    await vi.waitFor(async () => {
      expect(await indexedDbStorage.getItem('to-remove')).toBe('data')
    })

    indexedDbStorage.removeItem('to-remove')
    // removeItem 内部 void safeDel（异步），等待完成
    await vi.waitFor(async () => {
      expect(await indexedDbStorage.getItem('to-remove')).toBeNull()
    })
  })

  it('节流：窗口内多次 setItem 应合并为最后一次落盘', async () => {
    // 不用 fake timers（会冻结 IndexedDB 异步 Promise）。用真实定时器等待节流窗口
    // 三次快速写入
    indexedDbStorage.setItem('throttle-key', 'v1')
    indexedDbStorage.setItem('throttle-key', 'v2')
    indexedDbStorage.setItem('throttle-key', 'v3')

    // 等待节流窗口（PERSIST_THROTTLE_MS=1000）+ 落盘异步完成
    await vi.waitFor(
      async () => {
        const val = await indexedDbStorage.getItem('throttle-key')
        expect(val).toBe('v3') // 只落盘最终值，v1/v2 被合并丢弃
      },
      { timeout: 3000 },
    )
  })

  it('removeItem 取消挂起的节流写入，避免 remove 后又被写回', async () => {
    indexedDbStorage.setItem('cancel-key', 'will-be-removed')
    // removeItem 应取消挂起写入并删除
    indexedDbStorage.removeItem('cancel-key')
    // 等待足够时间，确认没有 trailing 写入把值写回
    await new Promise((r) => setTimeout(r, 1300))

    const val = await indexedDbStorage.getItem('cancel-key')
    expect(val).toBeNull()
  })

  it('容错降级：IndexedDB 不可用时回退内存模式且不抛异常', async () => {
    // 临时移除 indexedDB，触发降级
    const savedIdb = (globalThis as any).indexedDB
    Object.defineProperty(globalThis, 'indexedDB', {
      configurable: true,
      value: undefined,
    })
    vi.resetModules()
    const mod = await import('@/utils/indexedDbStorage')
    const degraded = mod.indexedDbStorage!

    // setItem 不应抛（走内存降级）
    expect(() => degraded.setItem('mem-key', 'mem-val')).not.toThrow()
    // 节流后内存降级写入（内存模式 safeSet 同步完成）
    mod.flushIndexedDbPersist()
    await new Promise((r) => setTimeout(r, 0))

    const val = await degraded.getItem('mem-key')
    expect(val).toBe('mem-val')

    // 恢复
    Object.defineProperty(globalThis, 'indexedDB', { configurable: true, value: savedIdb })
  })
})
