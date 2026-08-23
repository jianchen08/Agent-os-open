/**
 * useSchemaQuery 行为测试（query 化批次 2 核心验收）
 *
 * 验证 schema 缓存收敛与事件驱动强制新鲜两个契约：
 * 1. fetchSchemaCached 缓存新鲜直读（多消费方合流后零重复请求）；
 * 2. invalidateSchemaCache 后必发新请求（schema_updated/resync/插件启停路径）。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'

const mockGetSchema = vi.fn()

vi.mock('@/services/api/schema', () => ({
  getSchema: mockGetSchema,
}))

describe('useSchemaQuery helpers', () => {
  let fetchSchemaCached: typeof import('../useSchemaQuery')['fetchSchemaCached']
  let invalidateSchemaCache: typeof import('../useSchemaQuery')['invalidateSchemaCache']
  let queryClient: typeof import('@/services/query/queryClient')['queryClient']
  let queryKeys: typeof import('@/services/query/queryKeys')['queryKeys']

  beforeEach(async () => {
    vi.resetModules()
    mockGetSchema.mockReset()
    ;({ fetchSchemaCached, invalidateSchemaCache } = await import('../useSchemaQuery'))
    ;({ queryClient } = await import('@/services/query/queryClient'))
    ;({ queryKeys } = await import('@/services/query/queryKeys'))
    queryClient.clear()
  })

  it('首次拉取后窗口内重复取用零新请求（三处消费方合流）', async () => {
    mockGetSchema.mockResolvedValue({ tools: [], agents: [] })

    await fetchSchemaCached()
    await fetchSchemaCached()
    await fetchSchemaCached()

    expect(mockGetSchema).toHaveBeenCalledTimes(1)
    expect(queryClient.getQueryData(queryKeys.schema)).toEqual({ tools: [], agents: [] })
  })

  it('invalidateSchemaCache 后再取必发新请求（事件驱动强制新鲜）', async () => {
    mockGetSchema.mockResolvedValue({ version: 1 })
    await fetchSchemaCached()

    mockGetSchema.mockResolvedValue({ version: 2 })
    await invalidateSchemaCache()
    const refreshed = await fetchSchemaCached()

    expect(mockGetSchema).toHaveBeenCalledTimes(2)
    expect(refreshed).toEqual({ version: 2 })
  })
})
