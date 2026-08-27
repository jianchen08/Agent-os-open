/**
 * api/schema.ts 纯逻辑测试
 *
 * 覆盖 getWidgetsForPlugin 三种输入（缺失/null/非数组/合法数组）与
 * getSchema 端点调用（含重试包装）。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('../client', () => ({
  default: { get: mockGet },
}))

vi.mock('@/utils/retry', () => ({
  requestWithRetry: async (fn: () => any, _opts?: unknown) => fn(),
  isRetryableError: vi.fn().mockReturnValue(false),
}))

import { getSchema, getWidgetsForPlugin } from '@/services/api/schema'

describe('getWidgetsForPlugin - ui_schema 提取', () => {
  it('合法 widgets 数组 → 原样返回', () => {
    const widgets = [{ id: 'w1', type: 'review_document' }]
    expect(getWidgetsForPlugin({ widgets })).toEqual(widgets)
  })

  it('ui_schema 缺失 / null / widgets 非数组 → 空数组', () => {
    expect(getWidgetsForPlugin(undefined)).toEqual([])
    expect(getWidgetsForPlugin(null)).toEqual([])
    expect(getWidgetsForPlugin({})).toEqual([])
    expect(getWidgetsForPlugin({ widgets: 'not-array' as any })).toEqual([])
  })
})

describe('getSchema - 聚合 Schema 拉取', () => {
  beforeEach(() => {
    mockGet.mockReset()
  })

  it('调用 GET /api/v1/schema 并返回响应数据', async () => {
    const payload = {
      agents: [],
      pipelines: [],
      tools: [],
      routes: {},
      plugin_configs: [],
      plugin_contributes: [],
    }
    mockGet.mockResolvedValue({ data: payload })

    const result = await getSchema()
    expect(result).toEqual(payload)
    expect(mockGet).toHaveBeenCalledWith(expect.stringContaining('/schema'))
  })
})
