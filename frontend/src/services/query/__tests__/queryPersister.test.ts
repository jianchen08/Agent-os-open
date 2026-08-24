/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * queryPersister 测试
 *
 * 验证：IndexedDB 读写往返（fake-indexeddb 注入 + 真实 QueryClient dehydrate/hydrate）、
 * removeClient 清空、持久化失败静默降级不抛异常（隐私模式/配额耗尽场景）。
 * 刷新后缓存秒开依赖本层正确性。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { QueryClient, hydrate } from '@tanstack/react-query'
import { persistQueryClientSave } from '@tanstack/react-query-persist-client'
import fakeIndexedDB from 'fake-indexeddb'

;(globalThis as { indexedDB?: unknown }).indexedDB = fakeIndexedDB

describe('queryPersister', () => {
  let queryPersister: typeof import('@/services/query/queryPersister')['queryPersister']

  beforeEach(async () => {
    vi.resetModules()
    ;({ queryPersister } = await import('@/services/query/queryPersister'))
  })

  it('persist/restore 往返：hydrate 到新 QueryClient 后缓存数据可读', async () => {
    const source = new QueryClient()
    source.setQueryData(['sessions'], [{ id: 's1', title: '会话一' }])

    await persistQueryClientSave({ queryClient: source, persister: queryPersister, buster: 'test' })

    const persisted = await queryPersister.restoreClient()
    expect(persisted).toBeDefined()
    expect(persisted!.buster).toBe('test')

    const target = new QueryClient()
    hydrate(target, persisted!.clientState)
    expect(target.getQueryData(['sessions'])).toEqual([{ id: 's1', title: '会话一' }])
  })

  it('removeClient 清空持久化缓存', async () => {
    const source = new QueryClient()
    source.setQueryData(['agents'], [])
    await persistQueryClientSave({ queryClient: source, persister: queryPersister, buster: 'test' })

    await queryPersister.removeClient()

    const restored = await queryPersister.restoreClient()
    expect(restored).toBeUndefined()
  })

  it('IndexedDB 不可用时静默降级（不抛异常）', async () => {
    const realDB = (globalThis as { indexedDB?: unknown }).indexedDB
    // open 即失败：idb-keyval 的 get/set/del 全链 reject，验证 adapter 容错层
    ;(globalThis as { indexedDB?: unknown }).indexedDB = {
      open: () => {
        throw new Error('quota exceeded')
      },
    }
    try {
      const { queryPersisterStorage } = await import('@/services/query/queryPersister')

      // 读失败返回 null（回到无缓存冷启动）
      await expect(queryPersisterStorage.getItem('tanstack-query-cache')).resolves.toBeNull()
      // 写/删失败静默不抛
      await expect(queryPersisterStorage.setItem('k', 'v')).resolves.toBeUndefined()
      await expect(queryPersisterStorage.removeItem('k')).resolves.toBeUndefined()
    } finally {
      ;(globalThis as { indexedDB?: unknown }).indexedDB = realDB
    }
  })
})
