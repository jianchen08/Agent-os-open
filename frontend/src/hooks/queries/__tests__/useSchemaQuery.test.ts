/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * useSchemaQuery 行为测试（query 化批次 2 核心验收）
 *
 * 验证 schema 缓存收敛与事件驱动强制新鲜两个契约：
 * 1. fetchSchemaCached 缓存新鲜直读（多消费方合流后零重复请求）；
 * 2. invalidateSchemaCache 后必发新请求（schema_updated/resync/插件启停路径）。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import type * as useSchemaQueryMod from '../useSchemaQuery'
import type * as queryClientMod from '@/services/query/queryClient'
import type * as queryKeysMod from '@/services/query/queryKeys'

const mockGetSchema = vi.fn()

vi.mock('@/services/api/schema', () => ({
  getSchema: mockGetSchema,
}))

describe('useSchemaQuery helpers', () => {
  let fetchSchemaCached: useSchemaQueryMod['fetchSchemaCached']
  let invalidateSchemaCache: useSchemaQueryMod['invalidateSchemaCache']
  let queryClient: queryClientMod['queryClient']
  let queryKeys: queryKeysMod['queryKeys']

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
