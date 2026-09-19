// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** indexedDbStorage 容错 del：IndexedDB 删除失败 → 标记降级并删内存副本 */
import { describe, expect, it, vi } from 'vitest'

const { mockDel } = vi.hoisted(() => ({ mockDel: vi.fn() }))

vi.mock('idb-keyval', async (importOriginal) => {
  const actual = await importOriginal<typeof import('idb-keyval')>()
  return { ...actual, del: (...args: unknown[]) => mockDel(...args) }
})

describe('indexedDbStorage safeDel 失败降级', () => {
  it('del 抛错 → 标记内存降级并删除内存副本，不抛异常', async () => {
    mockDel.mockRejectedValue(new Error('idb down'))
    vi.resetModules()
    const mod = await import('@/utils/indexedDbStorage')
    const store = mod.indexedDbStorage!

    await store.setItem('del-key', 'val') // set 也走降级（del 失败前 set 可能成功；双保险断言）
    store.removeItem('del-key') // 同步触发、异步落（fire-and-forget）
    await new Promise((r) => setTimeout(r, 0))

    const val = await store.getItem('del-key')
    expect(val).toBeNull()
  })
})
