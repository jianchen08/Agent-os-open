/** @feature FP-0.2.二 前端持久化内存修复 | @ci: frontend-test */
/**
 * createDeferredIndexedDbStorage 测试（延迟序列化 PersistStorage）
 *
 * 背景（2026-11-12 内存事故）：zustand persist 在每次 setState 都调
 * storage.setItem，经 createJSONStorage 的路径会立即 JSON.stringify 整个
 * 快照——流式高频写入（每帧 + tool_progress 直写）下每秒数十次全量序列化，
 * 产生 GB 级短命大字符串（渲染进程 RSS 1.4~2.4GB 突刺的主因）。
 *
 * 契约：
 * - setItem 只登记最新快照引用（不序列化、不拷贝）；
 * - 节流 flush 时才 finalize（裁剪/trim）+ stringify + 落盘，窗口内只序列化一次；
 * - getItem/removeItem 语义与 indexedDbStorage 一致（roundtrip / 删除）。
 */
import fakeIndexedDB from 'fake-indexeddb'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type * as indexedDbStorageMod from '@/utils/indexedDbStorage'

// 注入 fake IndexedDB 到 globalThis，让 idb-keyval 在 jsdom 下走真实 IndexedDB 路径
;(globalThis as any).indexedDB = fakeIndexedDB

describe('createDeferredIndexedDbStorage', () => {
  let mod: typeof indexedDbStorageMod

  beforeEach(async () => {
    vi.resetModules()
    mod = await import('@/utils/indexedDbStorage')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('setItem 只登记引用：窗口内 N 次写入零 stringify，flush 一次序列化最终值', async () => {
    const stringifySpy = vi.spyOn(JSON, 'stringify')
    const storage = mod.createDeferredIndexedDbStorage<{ n: number }>()

    storage.setItem('defer-key', { state: { n: 1 }, version: 1 })
    storage.setItem('defer-key', { state: { n: 2 }, version: 1 })
    storage.setItem('defer-key', { state: { n: 3 }, version: 1 })

    // 登记阶段不发生任何序列化（延迟序列化的核心断言）
    expect(stringifySpy).not.toHaveBeenCalled()

    await vi.waitFor(
      async () => {
        const val = await storage.getItem('defer-key')
        expect(val).toEqual({ state: { n: 3 }, version: 1 })
      },
      { timeout: 3000 },
    )
    // 整个窗口只序列化一次（落盘那一次）
    expect(stringifySpy).toHaveBeenCalledTimes(1)
  })

  it('finalize 在 flush 时应用：持久化的是裁剪后的快照', async () => {
    const storage = mod.createDeferredIndexedDbStorage<{ big: string; keep: number }>((value) => ({
      state: { keep: value.state.keep },
      version: value.version,
    }))

    storage.setItem('trim-key', { state: { big: 'x'.repeat(1000), keep: 7 }, version: 1 })

    await vi.waitFor(
      async () => {
        const val = await storage.getItem('trim-key')
        expect(val).toEqual({ state: { keep: 7 }, version: 1 })
      },
      { timeout: 3000 },
    )
  })

  it('removeItem 删除落盘值且不被 trailing 写回', async () => {
    const storage = mod.createDeferredIndexedDbStorage<{ n: number }>()
    storage.setItem('defer-del', { state: { n: 1 }, version: 1 })
    await vi.waitFor(
      async () => {
        expect(await storage.getItem('defer-del')).toEqual({ state: { n: 1 }, version: 1 })
      },
      { timeout: 3000 },
    )

    storage.removeItem('defer-del')
    await new Promise((r) => setTimeout(r, 1300))
    expect(await storage.getItem('defer-del')).toBeNull()
  })

  it('getItem 读不存在的 key 返回 null', async () => {
    const storage = mod.createDeferredIndexedDbStorage<{ n: number }>()
    expect(await storage.getItem('defer-nonexistent-xyz')).toBeNull()
  })
})
