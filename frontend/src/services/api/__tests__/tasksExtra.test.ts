/**
 * 任务管理 API 补充测试（tasks.ts 未覆盖函数）
 *
 * 覆盖：任务列表/详情/删除/调试、项目详情/删除/暂停/恢复、根任务创建、
 * 容器任务列表、阶段产物、验收标准列表/结果、任务暂停/恢复/取消。
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

  describe('getTasks - 任务列表', () => {
    it('默认参数请求', async () => {
      const resp = { items: [taskInfo], total: 1 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.getTasks()

      expect(result.total).toBe(1)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks', {
        params: undefined,
      })
    })

    it('透传 skip/limit/status/session_id', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ items: [], total: 0 }))

      await taskApi.getTasks({ skip: 10, limit: 5, status: 'running', session_id: 's1' })

      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks', {
        params: { skip: 10, limit: 5, status: 'running', session_id: 's1' },
      })
    })
  })

  describe('getTask - 任务详情', () => {
    it('请求单任务端点并解包', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(taskInfo))

      const result = await taskApi.getTask('t1')

      expect(result.id).toBe('t1')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks/t1')
    })
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

  describe('getTasksDebug - 任务调试数据', () => {
    it('请求 debug/all 端点并透传参数', async () => {
      const resp = { items: [], total: 0 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.getTasksDebug({ skip: 0, limit: 10, sort_by: 'created_at', sort_order: 'desc' })

      expect(result).toEqual(resp)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks/debug/all', {
        params: { skip: 0, limit: 10, sort_by: 'created_at', sort_order: 'desc' },
      })
    })
  })

  describe('fetchProject - 项目详情', () => {
    it('解包 project 字段', async () => {
      const project = { id: 'p1', title: '项目1' }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ project }))

      const result = await taskApi.fetchProject('p1')

      expect(result).toEqual(project)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/projects/p1')
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

  describe('getContainerTasks - 容器任务列表', () => {
    it('按 session_id 请求并解包', async () => {
      const containers = [{ id: 'c1', title: '容器1' }]
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(containers))

      const result = await taskApi.getContainerTasks('s1')

      expect(result).toEqual(containers)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks/containers', {
        params: { session_id: 's1' },
      })
    })
  })

  describe('项目暂停/恢复/删除', () => {
    it('deleteProject DELETE 项目端点', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({}))

      await taskApi.deleteProject('p1')

      expect(apiClient.delete).toHaveBeenCalledWith('/ext/task_service/projects/p1')
    })

    it('pauseProject POST 暂停端点并解包', async () => {
      const project = { id: 'p1', title: '项目1' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ project }))

      const result = await taskApi.pauseProject('p1')

      expect(result.id).toBe('p1')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/projects/p1/pause')
    })

    it('resumeProject POST 恢复端点并解包', async () => {
      const project = { id: 'p1', title: '项目1' }
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ project }))

      const result = await taskApi.resumeProject('p1')

      expect(result.id).toBe('p1')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/task_service/projects/p1/resume')
    })
  })

  describe('fetchPhaseOutput - 阶段产物', () => {
    it('请求阶段产物端点', async () => {
      const resp = { phase: 'execute', output: { key: 'v' } }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.fetchPhaseOutput('t1', 'execute')

      expect(result.phase).toBe('execute')
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks/t1/phase/execute/output')
    })
  })

  describe('验收标准', () => {
    it('fetchTaskACs 请求 AC 列表端点', async () => {
      const resp = { items: [], total: 0 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(resp))

      const result = await taskApi.fetchTaskACs('t1')

      expect(result).toEqual(resp)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/task_service/tasks/t1/ac')
    })

    it('fetchACResult 解包 acceptance_criterion 字段', async () => {
      const ac = { id: 'ac1', title: 'AC1' }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ acceptance_criterion: ac }))

      const result = await taskApi.fetchACResult('t1', 'ac1')

      expect(result).toEqual(ac)
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/task_service/tasks/t1/ac/ac1/result',
      )
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
