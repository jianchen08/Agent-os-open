/**
 * 工作空间 API 服务测试
 *
 * 覆盖 /ext/workspace_service/workspaces/{container_task_id}* 端点封装：
 * 创建/删除/重命名/移动条目、文件内容读取。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as workspacesApi from '@/services/api/workspaces'

vi.mock('../client', () => {
  const mockClient = {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  }
  return { default: mockClient, apiClient: mockClient }
})

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

describe('工作空间 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('条目操作', () => {
    it('createEntry POST 文件/目录', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ success: true }))

      await workspacesApi.createEntry('t1', '/a/b.txt', 'file')

      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/t1/create-entry',
        { path: '/a/b.txt', type: 'file' },
      )
    })

    it('deleteEntry DELETE 带路径载荷', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({ success: true }))

      await workspacesApi.deleteEntry('t1', '/a/b.txt')

      expect(apiClient.delete).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/t1/entries',
        { data: { path: '/a/b.txt' } },
      )
    })

    it('renameEntry POST 旧路径与新名', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ success: true }))

      await workspacesApi.renameEntry('t1', '/a/old.txt', 'new.txt')

      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/t1/rename-entry',
        { old_path: '/a/old.txt', new_name: 'new.txt' },
      )
    })

    it('moveEntry POST 源路径与目标目录', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ success: true }))

      await workspacesApi.moveEntry('t1', '/a/b.txt', '/c')

      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/t1/move-entry',
        { source_path: '/a/b.txt', destination_dir: '/c' },
      )
    })
  })

  describe('getWorkspaceFileContent - 文件内容', () => {
    it('GET 文件内容端点并解包', async () => {
      const resp = { success: true, content: 'hello' }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await workspacesApi.getWorkspaceFileContent('t1', '/a/b.txt')

      expect(result.content).toBe('hello')
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/workspace_service/workspaces/t1/file-content',
        { params: { path: '/a/b.txt' } },
      )
    })
  })
})
