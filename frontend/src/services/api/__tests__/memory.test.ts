/**
 * 记忆 API 服务测试
 *
 * 测试情景记忆和语义记忆的管理接口
 * 与后端 /api/v1/memory/* 端点对齐
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  consolidateMemory,
  getEpisode,
  getEpisodes,
  getMemoryStats,
  getSemanticMemory,
  searchMemory,
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
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/memory/episodes', {
        params: { page: 1, page_size: 20 },
      })
    })

    it('应该支持分页参数', async () => {
      const mockResponse = { items: [], total: 0, page: 2, page_size: 10 }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      await getEpisodes(2, 10)

      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/memory/episodes', {
        params: { page: 2, page_size: 10 },
      })
    })
  })

  describe('getEpisode - 获取单个情景记忆', () => {
    it('应该成功获取情景记忆详情', async () => {
      const mockEpisode = {
        id: '1',
        intent_text: '代码重构',
        plan_dag: { nodes: [] },
        execution_summary: '成功完成',
        final_score: 0.9,
        tags: ['code'],
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockEpisode })

      const result = await getEpisode('1')

      expect(result).toEqual(mockEpisode)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/memory/episodes/1')
    })
  })

  describe('searchMemory - 搜索记忆', () => {
    it('应该成功搜索记忆（字符串查询）', async () => {
      const mockResponse = {
        items: [{ id: '1', content: '代码重构经验', score: 0.95 }],
        total: 1,
        query: '代码重构',
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await searchMemory('代码重构')

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/memory/search', {
        query: '代码重构',
        top_k: 10,
        min_score: 0.5,
      })
    })

    it('应该成功搜索记忆（对象查询）', async () => {
      const mockResponse = { items: [], total: 0, query: 'test' }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      await searchMemory({
        query: 'test',
        memory_types: ['episode'],
        top_k: 5,
        min_score: 0.7,
      })

      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/memory/search', {
        query: 'test',
        memory_types: ['episode'],
        top_k: 5,
        min_score: 0.7,
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
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/memory/semantic')
    })
  })

  describe('consolidateMemory - 记忆整合', () => {
    it('应该成功执行记忆整合', async () => {
      const mockResponse = {
        success: true,
        message: '整合完成',
        consolidated_count: 5,
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await consolidateMemory()

      expect(result).toEqual(mockResponse)
      expect(result.success).toBe(true)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/memory/consolidate')
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
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/memory/stats')
    })
  })

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
      const mockResponse = { items: [], total: 0, query: 'test' }
      vi.mocked(apiClient.post)
        .mockRejectedValueOnce({ response: { status: 503 } })
        .mockResolvedValueOnce({ data: mockResponse })

      const result = await searchMemory('test', { retry: true, maxRetries: 2 })

      expect(result).toEqual(mockResponse)
    })
  })
})
