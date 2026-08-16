/**
 * 工具 API 服务测试
 *
 * 仅覆盖 getTools——其余 CRUD 函数（getTool/generateTool/deleteTool/
 * getCodeEntry/searchCode/rollbackTool）指向后端不存在的端点，已删除
 * （2026-08 清理，详见 services/api/tools.ts 头注释）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { getTools } from '@/services/api/tools'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

describe('工具 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  describe('getTools - 获取工具列表', () => {
    it('应该成功获取工具列表', async () => {
      const mockResponse = {
        items: [
          {
            name: 'read_file',
            description: '读取文件',
            source: 'builtin',
            status: 'active',
          },
          {
            name: 'write_file',
            description: '写入文件',
            source: 'builtin',
            status: 'active',
          },
        ],
        total: 2,
        page: 1,
        page_size: 20,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      const result = await getTools()

      expect(result).toEqual(mockResponse)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tools', {
        params: expect.objectContaining({ page: 1, page_size: 20 }),
      })
    })

    it('应该支持分类和来源过滤', async () => {
      const mockResponse = { items: [], total: 0, page: 1, page_size: 20 }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResponse })

      await getTools({ category: 'file', source: 'builtin' })

      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tools', {
        params: expect.objectContaining({
          category: 'file',
          source: 'builtin',
        }),
      })
    })
  })
})
