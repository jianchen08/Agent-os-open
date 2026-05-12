/**
 * Agent API 服务测试
 *
 * 测试 Agent 的增删改查接口
 * 与后端 /api/v1/agents/* 端点对齐
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createAgent,
  deleteAgent,
  getAgent,
  getAgents,
  getDefaultAgent,
  updateAgent,
} from '@/services/api/agents'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

describe('Agent API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('getAgents - 获取 Agent 列表', () => {
    it('应该成功获取 Agent 列表', async () => {
      const mockResponse = {
        items: [
          { id: '1', name: 'Agent1', type: 'assistant', status: 'active' },
          { id: '2', name: 'Agent2', type: 'coder', status: 'active' },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      const result = await getAgents()

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/agents', {
        params: {
          page: 1,
          page_size: 20,
          status: undefined,
          agent_type: undefined,
          search: undefined,
        },
      })
    })

    it('应该支持分页和过滤参数', async () => {
      const mockResponse = { items: [], total: 0, page: 2, page_size: 10 }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      await getAgents({
        page: 2,
        pageSize: 10,
        status: 'active',
        type: 'coder',
      })

      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/agents', {
        params: {
          page: 2,
          page_size: 10,
          status: 'active',
          agent_type: 'coder',
          search: undefined,
        },
      })
    })
  })

  describe('getAgent - 获取单个 Agent', () => {
    it('应该成功获取 Agent 详情', async () => {
      const mockAgent = {
        id: '1',
        name: 'TestAgent',
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockAgent })

      const result = await getAgent('1')

      expect(result).toEqual(mockAgent)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/agents/1')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(getAgent('')).rejects.toThrow('Agent ID 不能为空')
    })
  })

  describe('createAgent - 创建 Agent', () => {
    it('应该成功创建 Agent', async () => {
      const createData = {
        name: 'NewAgent',
        description: '测试 Agent',
        model: 'gpt-4',
        system_prompt: '你是一个有用的助手',
      }
      const mockResponse = {
        id: '3',
        ...createData,
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await createAgent(createData)

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/agents', createData)
    })

    it('应该在名称为空时抛出错误', async () => {
      await expect(
        createAgent({ name: '', model: 'gpt-4', system_prompt: 'test' }),
      ).rejects.toThrow('Agent 名称不能为空')
    })
  })

  describe('updateAgent - 更新 Agent', () => {
    it('应该成功更新 Agent', async () => {
      const updateData = { name: 'UpdatedAgent' }
      const mockResponse = {
        id: '1',
        name: 'UpdatedAgent',
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: mockResponse })

      const result = await updateAgent('1', updateData)

      expect(result).toEqual(mockResponse)
      expect(apiClient.put).toHaveBeenCalledWith('/api/v1/agents/1', updateData)
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(updateAgent('', { name: 'Test' })).rejects.toThrow('Agent ID 不能为空')
    })
  })

  describe('deleteAgent - 删除 Agent', () => {
    it('应该成功删除 Agent', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce({ data: null })

      await deleteAgent('1')

      expect(apiClient.delete).toHaveBeenCalledWith('/api/v1/agents/1')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(deleteAgent('')).rejects.toThrow('Agent ID 不能为空')
    })
  })

  describe('getDefaultAgent - 获取默认 Agent', () => {
    it('应该成功获取默认 Agent', async () => {
      const mockAgent = {
        id: 'default',
        name: 'DefaultAgent',
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockAgent })

      const result = await getDefaultAgent()

      expect(result).toEqual(mockAgent)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/agents/default')
    })
  })

  describe('重试机制', () => {
    it('应该在网络错误时重试', async () => {
      const mockAgent = {
        id: '1',
        name: 'Agent',
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce({ data: mockAgent })

      const result = await getAgent('1', { retry: true, maxRetries: 2 })

      expect(result).toEqual(mockAgent)
      expect(apiClient.get).toHaveBeenCalledTimes(2)
    })

    it('应该在 5xx 错误时重试', async () => {
      const mockAgent = {
        id: '1',
        name: 'Agent',
        type: 'assistant',
        status: 'active',
      }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce({ response: { status: 500 } })
        .mockResolvedValueOnce({ data: mockAgent })

      const result = await getAgent('1', { retry: true, maxRetries: 2 })

      expect(result).toEqual(mockAgent)
    })

    it('应该在 4xx 错误时不重试', async () => {
      vi.mocked(apiClient.get).mockRejectedValue({ response: { status: 404 } })

      await expect(getAgent('1', { retry: true, maxRetries: 3 })).rejects.toThrow()
      expect(apiClient.get).toHaveBeenCalledTimes(1)
    })
  })
})
