/**
 * 记忆 API 服务测试
 *
 * 测试情景记忆和语义记忆的管理接口
 * 与后端 /ext/hindsight_memory_service/memory/* 端点对齐（4c 迁移后的 dispatcher 路径）
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  deleteMemoryById,
  getEpisodes,
  getMemoryById,
  getMemoryStats,
  getSemanticMemory,
} from '@/services/api/memory'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
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

  describe('getMemoryById - 获取单条记忆详情', () => {
    it('应该按 id 请求详情端点并返回详情', async () => {
      const mockDetail = {
        id: 'mem-1',
        content: '蓝鲸关键词记忆',
        memory_type: 'semantic',
        tags: ['session:thread-abc'],
        score: 0,
        created_at: '2026-09-20T00:00:00Z',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockDetail })

      const result = await getMemoryById('mem-1')

      expect(result).toEqual(mockDetail)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/mem-1')
    })

    it('不同 id 请求不同 URL（id 进路径）', async () => {
      vi.mocked(apiClient.get).mockResolvedValue({ data: { id: 'x', content: '', memory_type: '', tags: [], score: 0, created_at: '' } })

      await getMemoryById('mem-a')
      await getMemoryById('mem-b')

      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/mem-a')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/mem-b')
    })
  })

  describe('deleteMemoryById - 删除单条记忆', () => {
    it('应该按 id 发 DELETE 并返回消息', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce({ data: { message: '记忆已删除' } })

      const result = await deleteMemoryById('mem-1')

      expect(result).toEqual({ message: '记忆已删除' })
      expect(apiClient.delete).toHaveBeenCalledWith('/ext/hindsight_memory_service/memory/mem-1')
    })

    it('后端 404（未找到）应上抛给调用方', async () => {
      vi.mocked(apiClient.delete).mockRejectedValueOnce({ message: '未找到相关记忆' })

      await expect(deleteMemoryById('mem-gone')).rejects.toEqual({ message: '未找到相关记忆' })
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
      const mockStats = { episode_count: 3, knowledge_count: 1, total_count: 4 }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce({ response: { status: 503 } })
        .mockResolvedValueOnce({ data: mockStats })

      const result = await getMemoryStats({ retry: true, maxRetries: 2 })

      expect(result).toEqual(mockStats)
    })
  })
})
