/** @feature FP-MIGR 0.1→0.2迁移 @vision V6 可即用 @ci frontend-test */
/**
 * 任务管理 API 测试
 *
 * 测试任务执行闭环相关的 API 接口：
 * - 长期任务（项目）管理
 * - 短期任务阶段管理
 * - 验收标准（AC）评估
 *
 * 注：0.2 迁移后 tasks/projects/phases/ac 域已切 /ext/channel_api/*；
 *     completePreparePhase / evaluateAC / evaluateAllACs 三函数 0.2 已移除，其
 *     describe.skip 死骨架用例已删除（2026-08 清理）。
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as taskApi from '@/services/api/tasks'
import type { ProjectStatus, TaskPhase } from '@/services/api/../../types/task'
// Mock axios
vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    patch: vi.fn(),
    delete: vi.fn(),
  },
}))

 
import apiClient from '@/services/api/client'

describe('任务管理 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  // ========================================================================
  // 长期任务 API 测试
  // ========================================================================

  describe('fetchProjects - 获取长期任务列表', () => {
    it('应该成功获取长期任务列表', async () => {
      // 准备 mock 数据
      const mockResponse = {
        data: {
          items: [
            {
              id: 'project-1',
              user_id: 'user-1',
              session_id: 'session-1',
              goal: '实现用户认证模块',
              status: 'running' as ProjectStatus,
              auto_execute: true,
              current_task_index: 1,
              created_at: '2024-01-01T00:00:00Z',
              updated_at: '2024-01-01T01:00:00Z',
              metadata: {},
            },
            {
              id: 'project-2',
              user_id: 'user-1',
              session_id: 'session-2',
              goal: '优化数据库性能',
              status: 'suspended' as ProjectStatus,
              auto_execute: false,
              current_task_index: 0,
              created_at: '2024-01-02T00:00:00Z',
              updated_at: '2024-01-02T01:00:00Z',
              metadata: {},
            },
          ],
          total: 2,
          limit: 20,
          offset: 0,
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.fetchProjects({ page: 1, limit: 20 })

      // 验证结果
      expect(result.items).toHaveLength(2)
      expect(result.items[0].id).toBe('project-1')
      expect(result.items[0].goal).toBe('实现用户认证模块')
      expect(result.items[0].status).toBe('running')
      expect(result.total).toBe(2)

      // 验证 API 调用（0.2 迁移：/ext/channel_api/projects，params 走 axios params 对象）
      expect(apiClient.get).toHaveBeenCalledWith('/ext/channel_api/projects', {
        params: { page: 1, limit: 20 },
      })
      expect(apiClient.get).toHaveBeenCalledTimes(1)
    })

    it('应该支持状态过滤', async () => {
      const mockResponse = {
        data: {
          items: [
            {
              id: 'project-1',
              user_id: 'user-1',
              goal: '实现用户认证',
              status: 'running' as ProjectStatus,
              auto_execute: true,
              current_task_index: 1,
              created_at: '2024-01-01T00:00:00Z',
              updated_at: '2024-01-01T01:00:00Z',
            },
          ],
          total: 1,
          limit: 20,
          offset: 0,
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse)

      // 调用 API，过滤运行中的项目
      await taskApi.fetchProjects({ status: 'running' })

      // 验证 API 调用包含状态过滤（0.2 迁移：/ext/channel_api/projects）
      expect(apiClient.get).toHaveBeenCalledWith('/ext/channel_api/projects', {
        params: { status: 'running' },
      })
    })

    it('应该在请求失败时抛出错误', async () => {
      const mockError = {
        response: {
          status: 500,
          data: {
            detail: '服务器错误',
          },
        },
      }

      vi.mocked(apiClient.get).mockRejectedValueOnce(mockError)

      // 验证抛出错误
      await expect(taskApi.fetchProjects()).rejects.toThrow()
    })
  })

  describe('createProject - 创建长期任务', () => {
    it('应该成功创建长期任务', async () => {
      const mockResponse = {
        data: {
          project: {
            id: 'project-new',
            user_id: 'user-1',
            session_id: 'session-1',
            goal: '实现支付功能',
            status: 'planning' as ProjectStatus,
            auto_execute: true,
            current_task_index: 0,
            created_at: '2024-01-03T00:00:00Z',
            updated_at: '2024-01-03T00:00:00Z',
            metadata: {},
          },
        },
        status: 201,
        statusText: 'Created',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.createProject('实现支付功能', 'session-1', {
        autoExecute: true,
      })

      // 验证结果（impl 解包 response.data.project，字段为后端原样 snake_case）
      expect(result.id).toBe('project-new')
      expect(result.goal).toBe('实现支付功能')
      expect(result.status).toBe('planning')
      expect(result.auto_execute).toBe(true)

      // 验证 API 调用（0.2 迁移：/ext/channel_api/projects，body 用 snake_case）
      expect(apiClient.post).toHaveBeenCalledWith('/ext/channel_api/projects', {
        goal: '实现支付功能',
        session_id: 'session-1',
        auto_execute: true,
        metadata: undefined,
      })
    })

    it('应该在创建失败时抛出错误', async () => {
      const mockError = {
        response: {
          status: 400,
          data: {
            detail: '参数验证失败',
          },
        },
      }

      vi.mocked(apiClient.post).mockRejectedValueOnce(mockError)

      // 验证抛出错误
      await expect(taskApi.createProject('', 'session-1')).rejects.toThrow()
    })
  })

  describe('toggleProjectAutoExecute - 切换自动执行', () => {
    it('应该成功切换自动执行开关', async () => {
      // 0.2 实现：POST /ext/channel_api/projects/{id}/auto-execute { enabled }，返回 { project }
      const mockPostResponse = {
        data: {
          project: {
            id: 'project-1',
            user_id: 'user-1',
            goal: '实现用户认证',
            status: 'running' as ProjectStatus,
            auto_execute: true,
            current_task_index: 1,
            created_at: '2024-01-01T00:00:00Z',
            updated_at: '2024-01-01T02:00:00Z',
          },
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockPostResponse)

      // 调用 API
      const result = await taskApi.toggleProjectAutoExecute('project-1', true)

      // 验证结果（impl 解包 response.data.project）
      expect(result.auto_execute).toBe(true)

      // 验证 API 调用（POST 而非 PATCH；0.2 迁移路径）
      expect(apiClient.post).toHaveBeenCalledWith(
        '/ext/channel_api/projects/project-1/auto-execute',
        { enabled: true },
      )
    })
  })

  // ========================================================================
  // 任务阶段 API 测试
  // ========================================================================

  describe('fetchTaskPhase - 获取任务阶段状态', () => {
    it('应该成功获取任务阶段状态', async () => {
      const mockResponse = {
        data: {
          task_id: 'task-1',
          current_phase: 'execute' as TaskPhase,
          task_status: 'running',
          phases: {
            prepare: {
              status: 'completed',
              startTime: '2024-01-01T00:00:00Z',
              endTime: '2024-01-01T00:10:00Z',
              output: { plan: '执行计划' },
            },
            execute: {
              status: 'running',
              startTime: '2024-01-01T00:10:00Z',
            },
            evaluate: {
              status: 'pending',
            },
          },
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.fetchTaskPhase('task-1')

      // 验证结果（impl 原样返回 response.data，字段为后端 snake_case）
      expect(result.task_id).toBe('task-1')
      expect(result.current_phase).toBe('execute')
      expect(result.phases.prepare?.status).toBe('completed')
      expect(result.phases.execute?.status).toBe('running')

      // 验证 API 调用（0.2 迁移：/ext/channel_api/tasks/{id}/phase）
      expect(apiClient.get).toHaveBeenCalledWith('/ext/channel_api/tasks/task-1/phase')
    })
  })
})
