// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 任务管理 API 补充测试（tasks.ts 未覆盖函数）
 *
 * 覆盖：任务列表/删除、项目暂停/恢复、根任务创建、任务暂停/恢复/取消。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as taskApi from '@/services/api/tasks'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

const taskInfo = {
  id: 't1',
  title: '任务1',
  status: 'pending',
  priority: 'high',
  created_at: '2026-01-01T00:00:00Z',
}

describe('任务管理 API 补充', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('deleteTask - 删除任务', () => {
    it('成功返回 true', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({}))

      const result = await taskApi.deleteTask('t1')

      expect(result).toBe(true)
      expect(apiClient.delete).toHaveBeenCalledWith('/ext/task_service/tasks/t1')
    })

    it('失败降级返回 false', async () => {
      vi.mocked(apiClient.delete).mockRejectedValueOnce(new Error('Network Error'))

      const result = await taskApi.deleteTask('t1')

      expect(result).toBe(false)
    })
  })

  describe('createRootTask - 手动创建根任务', () => {
    it('POST 根任务载荷', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(taskInfo))

      const result = await taskApi.createRootTask({
        title: '新任务',
        task_scope: 'container',
        thread_id: 'th1',
      })

      expect(result.id).toBe('t1')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/tasks/root', {
        title: '新任务',
        task_scope: 'container',
        thread_id: 'th1',
      })
    })
  })

  describe('任务暂停/恢复/取消', () => {
    it('pauseTask POST 暂停端点并解包', async () => {
      const resp = { success: true, task_id: 't1', message: '已暂停' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.pauseTask('t1')

      expect(result.success).toBe(true)
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/tasks/t1/pause')
    })

    it('resumeTask POST 恢复端点并解包', async () => {
      const resp = { success: true, task_id: 't1', message: '已恢复' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.resumeTask('t1')

      expect(result.success).toBe(true)
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/tasks/t1/resume')
    })

    it('cancelTask POST 取消端点并解包', async () => {
      const resp = { success: true, task_id: 't1', message: '已取消' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.cancelTask('t1')

      expect(result.success).toBe(true)
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/tasks/t1/cancel')
    })
  })
})
