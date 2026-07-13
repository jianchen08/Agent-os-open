/**
 * 任务管理 API 测试
 *
 * 测试任务执行闭环相关的 API 接口：
 * - 长期任务（项目）管理
 * - 短期任务阶段管理
 * - 验收标准（AC）评估
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

      // 验证 API 调用
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/projects?page=1&limit=20')
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

      // 验证 API 调用包含状态过滤
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/projects?status=running')
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

      // 验证结果
      expect(result.id).toBe('project-new')
      expect(result.goal).toBe('实现支付功能')
      expect(result.status).toBe('planning')
      expect(result.autoExecute).toBe(true)

      // 验证 API 调用
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/projects', {
        goal: '实现支付功能',
        sessionId: 'session-1',
        autoExecute: true,
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
      const mockPatchResponse = {
        data: {
          project_id: 'project-1',
          auto_execute: true,
          updated_at: '2024-01-01T02:00:00Z',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      const mockGetResponse = {
        data: {
          id: 'project-1',
          user_id: 'user-1',
          goal: '实现用户认证',
          status: 'running' as ProjectStatus,
          auto_execute: true,
          current_task_index: 1,
          created_at: '2024-01-01T00:00:00Z',
          updated_at: '2024-01-01T02:00:00Z',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.patch).mockResolvedValueOnce(mockPatchResponse)
      vi.mocked(apiClient.get).mockResolvedValueOnce(mockGetResponse)

      // 调用 API
      const result = await taskApi.toggleProjectAutoExecute('project-1', true)

      // 验证结果
      expect(result.autoExecute).toBe(true)

      // 验证 API 调用
      expect(apiClient.patch).toHaveBeenCalledWith('/api/v1/projects/project-1/auto-execute', {
        enabled: true,
      })
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/projects/project-1')
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

      // 验证结果
      expect(result.taskId).toBe('task-1')
      expect(result.currentPhase).toBe('execute')
      expect(result.phaseStatus.prepare?.status).toBe('completed')
      expect(result.phaseStatus.execute?.status).toBe('running')

      // 验证 API 调用
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tasks/task-1/phase')
    })
  })

  describe('completePreparePhase - 完成准备阶段', () => {
    it('应该成功完成准备阶段', async () => {
      const mockResponse = {
        data: {
          task_id: 'task-1',
          current_phase: 'execute' as TaskPhase,
          task_status: 'running',
          completed_at: '2024-01-01T00:10:00Z',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.completePreparePhase('task-1', {
        plan: '执行计划',
        research: '调研报告',
      })

      // 验证结果
      expect(result.taskId).toBe('task-1')
      expect(result.currentPhase).toBe('execute')

      // 验证 API 调用
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tasks/task-1/phase/prepare/complete', {
        output: {
          plan: '执行计划',
          research: '调研报告',
        },
      })
    })
  })

  describe('fetchPhaseOutput - 获取阶段产物', () => {
    it('应该成功获取阶段产物', async () => {
      const mockResponse = {
        data: {
          task_id: 'task-1',
          phase: 'prepare' as TaskPhase,
          status: 'completed',
          output: {
            plan: '执行计划',
            research: '调研报告',
            subtasks: ['任务1', '任务2'],
          },
          start_time: '2024-01-01T00:00:00Z',
          end_time: '2024-01-01T00:10:00Z',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.fetchPhaseOutput('task-1', 'prepare')

      // 验证结果
      expect(result.output).toEqual({
        plan: '执行计划',
        research: '调研报告',
        subtasks: ['任务1', '任务2'],
      })
      expect(result.error).toBeUndefined()

      // 验证 API 调用
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tasks/task-1/phase/prepare/output')
    })
  })

  // ========================================================================
  // AC 评估 API 测试
  // ========================================================================

  describe('fetchTaskACs - 获取任务验收标准', () => {
    it('应该成功获取任务验收标准列表', async () => {
      const mockResponse = {
        data: {
          task_id: 'task-1',
          total: 3,
          passed: 1,
          failed: 0,
          pending: 2,
          acceptance_criteria: [
            {
              id: 'ac-1',
              description: '支持用户名密码登录',
              type: 'functional',
              is_red_line: true,
              weight: 1.0,
              status: 'passed',
              evaluator_type: 'tool',
              evaluator_id: 'test_runner',
              evaluated_at: '2024-01-01T00:15:00Z',
              retry_count: 0,
              evaluation_result: {
                passed: true,
                message: '所有测试用例通过',
              },
            },
            {
              id: 'ac-2',
              description: '支持 JWT Token 认证',
              type: 'functional',
              is_red_line: true,
              weight: 1.0,
              status: 'pending',
              evaluator_type: 'tool',
              evaluator_id: 'test_runner',
            },
            {
              id: 'ac-3',
              description: '通过安全测试',
              type: 'security',
              is_red_line: false,
              weight: 0.8,
              status: 'pending',
              evaluator_type: 'agent',
              evaluator_id: 'security_checker',
            },
          ],
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce(mockResponse)

      // 调用 API
      const result = await taskApi.fetchTaskACs('task-1')

      // 验证结果
      expect(result.taskId).toBe('task-1')
      expect(result.acceptanceCriteria).toHaveLength(3)
      expect(result.acceptanceCriteria[0].id).toBe('ac-1')
      expect(result.acceptanceCriteria[0].description).toBe('支持用户名密码登录')
      expect(result.acceptanceCriteria[0].status).toBe('passed')
      expect(result.acceptanceCriteria[0].isRedLine).toBe(true)

      // 验证 API 调用
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tasks/task-1/ac')
    })
  })

  describe('evaluateAC - 评估单个验收标准', () => {
    it('应该成功评估验收标准', async () => {
      // Mock 评估请求响应
      const mockEvaluateResponse = {
        data: {
          task_id: 'task-1',
          ac_id: 'ac-2',
          passed: true,
          score: 100,
          feedback: '验收标准已通过',
          details: {
            test_results: '所有测试通过',
          },
          execution_time: 1500,
          evaluated_at: '2024-01-01T00:20:00Z',
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      // Mock 获取 AC 列表响应
      const mockACListResponse = {
        data: {
          task_id: 'task-1',
          total: 3,
          passed: 2,
          failed: 0,
          pending: 1,
          acceptance_criteria: [
            {
              id: 'ac-2',
              description: '支持 JWT Token 认证',
              type: 'functional',
              is_red_line: true,
              weight: 1.0,
              status: 'passed',
              evaluator_type: 'tool',
              evaluator_id: 'test_runner',
              evaluated_at: '2024-01-01T00:20:00Z',
              retry_count: 0,
              evaluation_result: {
                passed: true,
                message: '验收标准已通过',
              },
            },
          ],
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockEvaluateResponse)
      vi.mocked(apiClient.get).mockResolvedValueOnce(mockACListResponse)

      // 调用 API
      const result = await taskApi.evaluateAC('task-1', 'ac-2', {
        test_results: '测试数据',
      })

      // 验证结果
      expect(result.id).toBe('ac-2')
      expect(result.status).toBe('passed')
      expect(result.result?.passed).toBe(true)

      // 验证 API 调用
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tasks/task-1/ac/ac-2/evaluate', {
        evidence: {
          test_results: '测试数据',
        },
      })
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tasks/task-1/ac')
    })
  })

  describe('evaluateAllACs - 评估所有验收标准', () => {
    it('应该成功评估所有验收标准', async () => {
      // Mock 评估所有请求响应
      const mockEvaluateAllResponse = {
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      // Mock 获取 AC 列表响应
      const mockACListResponse = {
        data: {
          task_id: 'task-1',
          total: 3,
          passed: 3,
          failed: 0,
          pending: 0,
          acceptance_criteria: [
            {
              id: 'ac-1',
              description: '支持用户名密码登录',
              type: 'functional',
              status: 'passed',
              evaluator_type: 'tool',
              evaluator_id: 'test_runner',
              evaluated_at: '2024-01-01T00:20:00Z',
            },
            {
              id: 'ac-2',
              description: '支持 JWT Token 认证',
              type: 'functional',
              status: 'passed',
              evaluator_type: 'tool',
              evaluator_id: 'test_runner',
              evaluated_at: '2024-01-01T00:20:00Z',
            },
            {
              id: 'ac-3',
              description: '通过安全测试',
              type: 'security',
              status: 'passed',
              evaluator_type: 'agent',
              evaluator_id: 'security_checker',
              evaluated_at: '2024-01-01T00:20:00Z',
            },
          ],
        },
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce(mockEvaluateAllResponse)
      vi.mocked(apiClient.get).mockResolvedValueOnce(mockACListResponse)

      // 调用 API
      const results = await taskApi.evaluateAllACs('task-1')

      // 验证结果
      expect(results).toHaveLength(3)
      expect(results.every((ac) => ac.status === 'passed')).toBe(true)

      // 验证 API 调用
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/tasks/task-1/ac/evaluate-all')
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/tasks/task-1/ac')
    })
  })
})
