/**
 * Agent API 服务测试
 *
 * 仅覆盖 agent_manager 插件真实端点：GET /ext/agent_manager/agents（列表）。
 * getAgent/createAgent/updateAgent/deleteAgent/getDefaultAgent 指向
 * 后端不存在的端点，已删除（2026-08 清理，详见 services/api/agents.ts 头注释）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getAgents } from '@/services/api/agents'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
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
      expect(apiClient.get).toHaveBeenCalledWith('/ext/agent_manager/agents', {
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

      expect(apiClient.get).toHaveBeenCalledWith('/ext/agent_manager/agents', {
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

  describe('重试机制', () => {
    it('应该在网络错误时重试', async () => {
      const mockResponse = { items: [], total: 0, page: 1, page_size: 20 }

      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce({ data: mockResponse })

      const result = await getAgents({}, { retry: true, maxRetries: 2 })

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledTimes(2)
    })

    it('应该在 5xx 错误时重试', async () => {
      const mockResponse = { items: [], total: 0, page: 1, page_size: 20 }

      vi.mocked(apiClient.get)
        .mockRejectedValueOnce({ response: { status: 500 } })
        .mockResolvedValueOnce({ data: mockResponse })

      const result = await getAgents({}, { retry: true, maxRetries: 2 })

      expect(result).toEqual(mockResponse)
    })

    it('应该在 4xx 错误时不重试', async () => {
      vi.mocked(apiClient.get).mockRejectedValue({ response: { status: 404 } })

      await expect(getAgents({}, { retry: true, maxRetries: 3 })).rejects.toThrow()
      expect(apiClient.get).toHaveBeenCalledTimes(1)
    })
  })
})
