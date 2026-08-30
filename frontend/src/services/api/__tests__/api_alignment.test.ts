/** @feature FP-MIGR 0.1→0.2迁移 @vision V6 可即用 @ci frontend-test */
/**
 * 前后端 API 对齐修复测试
 *
 * 验证前端 API 调用与后端接口的对齐修复项：
 * - F10: longTermTasks pause/resume/toggleAutoExecute 使用 PATCH
 * - F11: fetchLongTermTasks 不发送后端不支持的参数
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

// ============================================================================
// 收尾闸（channel_api 退役批次 5）：前端 /ext/channel_api 零命中 + 常量层不手写 /ext
// ============================================================================

const FRONTEND_SRC = path.resolve(__dirname, '../../..')

describe('收尾闸 - channel_api 退役（批次 5）', () => {
  it('frontend/src 生产代码零命中 "ext/channel_api"（排除生成物与测试）', () => {
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name)
        if (entry.isDirectory()) {
          if (entry.name === 'node_modules' || entry.name === '__tests__') continue
          walk(full)
        } else if (/\.(ts|tsx)$/.test(entry.name)) {
          if (/\.(test|spec)\./.test(entry.name)) continue
          if (full.endsWith('endpoints.generated.ts')) continue
          const text = fs.readFileSync(full, 'utf-8')
          if (text.includes('ext/channel_api')) {
            offenders.push(path.relative(FRONTEND_SRC, full).replace(/\\/g, '/'))
          }
        }
      }
    }
    walk(FRONTEND_SRC)
    expect(offenders).toEqual([])
  })

  it('constants/api.ts 不再手写 /ext/ 字符串字面量（插件端点一律经生成物投影）', () => {
    const text = fs.readFileSync(path.resolve(FRONTEND_SRC, 'constants/api.ts'), 'utf-8')
    const extLiterals = [...text.matchAll(/['"`]\/ext\//g)]
    expect(extLiterals.map((m) => m.index)).toEqual([])
  })
})

// 用 vi.hoisted 创建 mock 函数，确保在 vi.mock 提升时可用
const { mockGet, mockPost, mockPatch, mockPut, mockDelete } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPost: vi.fn(),
  mockPatch: vi.fn(),
  mockPut: vi.fn(),
  mockDelete: vi.fn(),
}))

// Mock client 模块 — 同时提供 default 和 { apiClient } 命名导出
// tasks.ts 使用 import apiClient (default)，longTermTasks.ts 使用 import { apiClient }
vi.mock('../client', () => ({
  default: {
    get: mockGet,
    post: mockPost,
    patch: mockPatch,
    put: mockPut,
    delete: mockDelete,
  },
  apiClient: {
    get: mockGet,
    post: mockPost,
    patch: mockPatch,
    put: mockPut,
    delete: mockDelete,
  },
}))

import * as taskApi from '@/services/api/tasks'
import * as longTermTasksApi from '@/services/api/longTermTasks'

// ============================================================================
// F10: longTermTasks HTTP 方法测试
// ============================================================================

describe('F10 - longTermTasks 使用 PATCH 方法', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('pauseLongTermTask 应使用 PATCH 而非 PUT', async () => {
    const mockResponse = {
      data: {
        id: 'task-1',
        title: '长期任务',
        status: 'blocked',
        tags: ['long-term'],
        created_at: '2026-05-14T00:00:00Z',
        updated_at: '2026-05-14T01:00:00Z',
      },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockPatch.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.pauseLongTermTask('task-1')

    // 验证使用的是 PATCH
    expect(mockPatch).toHaveBeenCalledWith('/ext/task_service/tasks/task-1', {
      status: 'blocked',
    })
    // 验证没有使用 PUT
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('resumeLongTermTask 应使用 PATCH 而非 PUT', async () => {
    const mockResponse = {
      data: {
        id: 'task-1',
        title: '长期任务',
        status: 'running',
        tags: ['long-term'],
        created_at: '2026-05-14T00:00:00Z',
        updated_at: '2026-05-14T01:00:00Z',
      },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockPatch.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.resumeLongTermTask('task-1')

    // 验证使用的是 PATCH
    expect(mockPatch).toHaveBeenCalledWith('/ext/task_service/tasks/task-1', {
      status: 'running',
    })
    // 验证没有使用 PUT
    expect(mockPut).not.toHaveBeenCalled()
  })

  it('toggleAutoExecute 应使用 PATCH 而非 PUT', async () => {
    // Mock get 任务
    const mockGetResponse = {
      data: {
        id: 'task-1',
        title: '长期任务',
        status: 'running',
        tags: ['long-term'],
        created_at: '2026-05-14T00:00:00Z',
        updated_at: '2026-05-14T01:00:00Z',
      },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    // Mock patch 更新
    const mockPatchResponse = {
      data: {
        id: 'task-1',
        title: '长期任务',
        status: 'running',
        tags: ['long-term', 'auto-execute'],
        created_at: '2026-05-14T00:00:00Z',
        updated_at: '2026-05-14T02:00:00Z',
      },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockGetResponse)
    mockPatch.mockResolvedValueOnce(mockPatchResponse)

    await longTermTasksApi.toggleAutoExecute('task-1', true)

    // 验证使用 PATCH 更新标签
    expect(mockPatch).toHaveBeenCalledWith('/ext/task_service/tasks/task-1', {
      tags: ['long-term', 'auto-execute'],
    })
    // 验证没有使用 PUT
    expect(mockPut).not.toHaveBeenCalled()
  })
})

// ============================================================================
// F11: fetchLongTermTasks 参数兼容性测试
// ============================================================================

describe('F11 - fetchLongTermTasks 参数兼容性', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('应仅发送后端支持的参数（skip, limit）', async () => {
    const mockResponse = {
      data: { items: [], total: 0 },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.fetchLongTermTasks({ page: 1, limit: 20 })

    // 获取实际调用的 URL
    expect(mockGet).toHaveBeenCalledTimes(1)
    const callUrl = mockGet.mock.calls[0][0] as string

    // 验证使用 skip 参数（由 page 和 limit 计算得出）
    expect(callUrl).toContain('skip=0')
    expect(callUrl).toContain('limit=20')

    // 验证不包含后端不支持的参数
    expect(callUrl).not.toContain('page=')
    expect(callUrl).not.toContain('sort_by=')
    expect(callUrl).not.toContain('priority=')
  })

  it('应正确将 page 转换为 skip', async () => {
    const mockResponse = {
      data: { items: [], total: 0 },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.fetchLongTermTasks({ page: 3, limit: 10 })

    const callUrl = mockGet.mock.calls[0][0] as string

    // page=3, limit=10 → skip=(3-1)*10=20
    expect(callUrl).toContain('skip=20')
    expect(callUrl).toContain('limit=10')
  })

  it('应支持 status 参数传递', async () => {
    const mockResponse = {
      data: { items: [], total: 0 },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.fetchLongTermTasks({ status: 'running' as any })

    const callUrl = mockGet.mock.calls[0][0] as string

    expect(callUrl).toContain('status=running')
  })

  it('应正确解包后端 {items, total} 并过滤长期任务', async () => {
    const mockResponse = {
      data: {
        items: [
          {
            id: 'task-1',
            title: '长期任务A',
            status: 'running',
            tags: ['long-term', 'auto-execute'],
            created_at: '2026-05-14T00:00:00Z',
          },
          {
            id: 'task-2',
            title: '普通任务B',
            status: 'pending',
            tags: [],
            created_at: '2026-05-14T01:00:00Z',
          },
          {
            id: 'task-3',
            title: '长期任务C',
            status: 'completed',
            tags: ['long-term'],
            created_at: '2026-05-14T02:00:00Z',
          },
        ],
        total: 3,
      },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockResponse)

    const result = await longTermTasksApi.fetchLongTermTasks({ limit: 100 })

    // 应过滤出带 'long-term' 标签的任务
    expect(result.items).toHaveLength(2)
    expect(result.total).toBe(2)
    expect(result.items[0].id).toBe('task-1')
    expect(result.items[1].id).toBe('task-3')
  })

  it('使用默认参数时 skip 应为 0，limit 应为 100', async () => {
    const mockResponse = {
      data: { items: [], total: 0 },
      status: 200,
      statusText: 'OK',
      headers: {},
      config: {} as any,
    }

    mockGet.mockResolvedValueOnce(mockResponse)

    await longTermTasksApi.fetchLongTermTasks()

    const callUrl = mockGet.mock.calls[0][0] as string

    // 默认 page=1, limit=100 → skip=0
    expect(callUrl).toContain('skip=0')
    expect(callUrl).toContain('limit=100')
  })
})

// ============================================================================
// 综合：前后端 Projects API 响应解包验证
// ============================================================================

describe('前后端 Projects API 响应解包验证', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('fetchProjects 应返回 {items, total} 结构', async () => {
    const mockResponse = {
      data: {
        items: [
          {
            id: 'project-1',
            goal: '实现用户认证模块',
            status: 'running',
            auto_execute: true,
          },
          {
            id: 'project-2',
            goal: '优化数据库性能',
            status: 'suspended',
            auto_execute: false,
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

    mockGet.mockResolvedValueOnce(mockResponse)

    const result = await taskApi.fetchProjects({ page: 1, limit: 20 })

    // 验证返回 {items, total} 结构
    expect(result.items).toHaveLength(2)
    expect(result.total).toBe(2)
    expect(result.items[0].id).toBe('project-1')

    // 验证调用端点（实际代码使用 { params } 对象格式）
    expect(mockGet).toHaveBeenCalledWith('/ext/task_service/projects', {
      params: { page: 1, limit: 20 },
    })
  })

  it('createProject 应正确解包 {project: {...}} 响应', async () => {
    const mockResponse = {
      data: {
        project: {
          id: 'proj-1',
          user_id: 'user-1',
          session_id: 'session-1',
          goal: '测试目标',
          status: 'planning',
          auto_execute: false,
          current_task_index: 0,
          created_at: '2026-05-14T00:00:00Z',
          updated_at: '2026-05-14T00:00:00Z',
          metadata: {},
        },
      },
      status: 201,
      statusText: 'Created',
      headers: {},
      config: {} as any,
    }

    mockPost.mockResolvedValueOnce(mockResponse)

    const result = await taskApi.createProject('测试目标', 'session-1', {
      autoExecute: true,
    })

    // 验证解包了 project 字段
    expect(result.id).toBe('proj-1')
    expect(result.goal).toBe('测试目标')
    expect(result.status).toBe('planning')

    // 验证调用端点
    expect(mockPost).toHaveBeenCalledWith('/ext/task_service/projects', {
      goal: '测试目标',
      session_id: 'session-1',
      auto_execute: true,
      metadata: undefined,
    })
  })
})
