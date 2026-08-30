/**
 * 记忆 API 服务测试
 *
 * 测试情景记忆和语义记忆的管理接口
 * 与后端 /ext/hindsight_memory_service/memory/* 端点对齐（4c 迁移后的 dispatcher 路径）
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getEpisodes,
  getMemoryStats,
  getSemanticMemory,
} from '@/services/api/memory'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

 
import apiClient from '@/services/api/client'

describe('记忆 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('getEpisodes - 获取情景记忆列表', () => {
    it('应该成功获取情景记忆列表', async () => {
      const mockResponse = {
        items: [
          {
            id: '1',
            intent_text: '代码重构',
            final_score: 0.9,
            tags: ['code'],
          },
          {
            id: '2',
            intent_text: '测试编写',
            final_score: 0.85,
            tags: ['test'],
          },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      const result = await getEpisodes()

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/episodes', {
        params: { page: 1, page_size: 20 },
      })
    })

    it('应该支持分页参数', async () => {
      const mockResponse = { items: [], total: 0, page: 2, page_size: 10 }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      await getEpisodes(2, 10)

      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/episodes', {
        params: { page: 2, page_size: 10 },
      })
    })
  })

  describe('getSemanticMemory - 获取语义记忆', () => {
    it('应该成功获取语义记忆列表', async () => {
      const mockResponse = {
        items: [{ id: '1', content: 'Python 最佳实践', source_type: 'document' }],
        total: 1,
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      const result = await getSemanticMemory()

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/semantic')
    })
  })

  describe('getMemoryStats - 获取记忆统计', () => {
    it('应该成功获取记忆统计数据', async () => {
      const mockStats = {
        episode_count: 100,
        knowledge_count: 50,
        total_count: 150,
        last_updated: '2024-01-01T00:00:00Z',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockStats })

      const result = await getMemoryStats()

      expect(result).toEqual(mockStats)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/stats')
    })
  })

  // importDocument 用例已删除：函数指向后端不存在的 /memory/import 端点（2026-08 清理）

  describe('重试机制', () => {
    it('应该在网络错误时重试', async () => {
      const mockStats = {
        episode_count: 10,
        knowledge_count: 5,
        total_count: 15,
      }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce({ data: mockStats })

      const result = await getMemoryStats({ retry: true, maxRetries: 2 })

      expect(result).toEqual(mockStats)
      expect(apiClient.get).toHaveBeenCalledTimes(2)
    })

    it('应该在 5xx 错误时重试', async () => {
      const mockStats = { episode_count: 3, knowledge_count: 1, total_count: 4 }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce({ response: { status: 503 } })
        .mockResolvedValueOnce({ data: mockStats })

      const result = await getMemoryStats({ retry: true, maxRetries: 2 })

      expect(result).toEqual(mockStats)
    })
  })
})
