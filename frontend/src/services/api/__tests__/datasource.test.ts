/**
 * 动态数据源 API 测试
 *
 * 覆盖 AC-11-3: 支持动态数据源（调用内核代理端点获取选项列表）
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fetchDynamicDataSource } from '@/services/api/datasource'

// Mock apiClient
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

describe('fetchDynamicDataSource — AC-11-3: 动态数据源', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('调用 /api/v1/datasource/{uri} 端点', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockResolvedValue({
      data: {
        success: true,
        options: [
          { label: '选项A', value: 'a' },
          { label: '选项B', value: 'b' },
        ],
      },
    })

    const result = await fetchDynamicDataSource('categories/list')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/datasource/categories/list')
    expect(result.success).toBe(true)
    expect(result.options).toHaveLength(2)
  })

  it('传递 params 参数', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockResolvedValue({
      data: { success: true, options: [] },
    })

    await fetchDynamicDataSource('tools/list', { category: 'search' })

    expect(mockGet).toHaveBeenCalledWith('/api/v1/datasource/tools/list', {
      params: { category: 'search' },
    })
  })

  it('API 返回失败时 success=false', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockResolvedValue({
      data: { success: false },
    })

    const result = await fetchDynamicDataSource('invalid/source')

    expect(result.success).toBe(false)
    expect(result.options).toBeUndefined()
  })

  it('网络错误时抛出异常', async () => {
    const mockGet = vi.mocked(apiClient.get)
    mockGet.mockRejectedValue(new Error('Network error'))

    await expect(fetchDynamicDataSource('any/source')).rejects.toThrow('Network error')
  })
})
