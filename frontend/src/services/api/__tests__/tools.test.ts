/**
 * 工具 API 服务测试
 *
 * 测试工具的查询、生成、删除等接口
 * 与后端 /api/v1/tools/* 端点对齐
 */

/* eslint-disable import-x/order */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  deleteTool,
  generateTool,
  getCodeEntry,
  getTool,
  getTools,
  rollbackTool,
  searchCode,
} from '@/services/api/tools'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
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

  describe('getTool - 获取单个工具', () => {
    it('应该成功获取工具详情', async () => {
      const mockTool = {
        name: 'read_file',
        description: '读取文件',
        source: 'builtin',
        status: 'active',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockTool })

      const result = await getTool('read_file')

      expect(result).toEqual(mockTool)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tools/read_file')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(getTool('')).rejects.toThrow('工具 ID 不能为空')
    })
  })

  describe('generateTool - 生成工具', () => {
    it('应该成功生成工具', async () => {
      const generateData = { name: 'custom_tool', description: '自定义工具' }
      const mockResponse = {
        ...generateData,
        source: 'custom',
        status: 'active',
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockResponse })

      const result = await generateTool(generateData)

      expect(result).toEqual(mockResponse)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tools/generate', generateData)
    })

    it('应该在名称为空时抛出错误', async () => {
      await expect(generateTool({ name: '', description: 'test' })).rejects.toThrow(
        '工具名称不能为空',
      )
    })
  })

  describe('deleteTool - 删除工具', () => {
    it('应该成功删除工具', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce({ data: null })

      await deleteTool('custom_tool')

      expect(apiClient.delete).toHaveBeenCalledWith('/api/v1/tools/custom_tool')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(deleteTool('')).rejects.toThrow('工具 ID 不能为空')
    })
  })

  describe('getCodeEntry - 获取代码条目', () => {
    it('应该成功获取代码条目', async () => {
      const mockEntry = {
        id: '1',
        code: 'def test(): pass',
        language: 'python',
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockEntry })

      const result = await getCodeEntry('1')

      expect(result).toEqual(mockEntry)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tools/code/1')
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(getCodeEntry('')).rejects.toThrow('条目 ID 不能为空')
    })
  })

  describe('searchCode - 搜索代码', () => {
    it('应该成功搜索代码', async () => {
      const mockResult = { items: [], total: 0 }
      vi.mocked(apiClient.get).mockResolvedValueOnce({ data: mockResult })

      const result = await searchCode('function')

      expect(result).toEqual(mockResult)
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tools/code', {
        params: { query: 'function' },
      })
    })

    it('应该在查询为空时抛出错误', async () => {
      await expect(searchCode('')).rejects.toThrow('搜索关键词不能为空')
    })
  })

  describe('rollbackTool - 回滚工具版本', () => {
    it('应该成功回滚工具', async () => {
      const mockTool = {
        name: 'custom_tool',
        version: 1,
        source: 'custom',
        status: 'active',
      }
      vi.mocked(apiClient.post).mockResolvedValueOnce({ data: mockTool })

      const result = await rollbackTool('custom_tool', 1)

      expect(result).toEqual(mockTool)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tools/custom_tool/rollback', {
        version: 1,
      })
    })

    it('应该在 ID 为空时抛出错误', async () => {
      await expect(rollbackTool('')).rejects.toThrow('工具 ID 不能为空')
    })
  })
})
