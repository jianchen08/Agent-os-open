/**
 * 长期任务 API 路径迁移回归测试
 *
 * 背景：useTaskPolling（router.tsx HomePage 挂载即启用，默认 5s 轮询）→ fetchTasks
 * → longTermTasks.ts 硬编码 `GET /api/v1/tasks`。内核（kernel server.rs）无 /api/v1/tasks
 * 路由 → axum fallback 返回空 body 404 → 前端控制台持续 `[VALIDATION] 404`（errorReporting）。
 * 修复：longTermTasks.ts 全部端点迁移到 /ext/channel_api/tasks（插件 channel_api 已实现该域）。
 *
 * 本测试断言可观察行为：所有长期任务 API 请求必须使用 /ext/channel_api/tasks 前缀，
 * 防止回退到 /api/v1/tasks（内核无路由 → 404 复发）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Mock apiClient（外部依赖），捕获实际请求 URL
// longTermTasks.ts 使用命名导入 `import { apiClient } from '@/services/api/client'`，
// 故 mock 需同时提供 default 与 apiClient 命名导出。
const getMock = vi.fn()
const patchMock = vi.fn()
const postMock = vi.fn()
const deleteMock = vi.fn()

vi.mock('../client', () => {
  const mockClient = {
    get: (...args: unknown[]) => getMock(...args),
    patch: (...args: unknown[]) => patchMock(...args),
    post: (...args: unknown[]) => postMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  }
  return { default: mockClient, apiClient: mockClient }
})

import * as longTermTaskApi from '@/services/api/longTermTasks'

describe('longTermTasks API 端点路径（4c 迁移：/ext/channel_api/tasks）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    getMock.mockResolvedValue({ data: { items: [], total: 0 } })
    patchMock.mockResolvedValue({ data: { id: 't1', tags: [] } })
    postMock.mockResolvedValue({ data: { id: 't1' } })
    deleteMock.mockResolvedValue({})
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  it('fetchLongTermTasks 请求 /ext/channel_api/tasks 而非 /api/v1/tasks', async () => {
    await longTermTaskApi.fetchLongTermTasks({ page: 1, limit: 100 })

    expect(getMock).toHaveBeenCalledTimes(1)
    const [url] = getMock.mock.calls[0]
    expect(url).toContain('/ext/channel_api/tasks')
    expect(url).not.toContain('/api/v1/tasks')
    // 查询参数沿用后端支持的 skip/limit/status
    expect(url).toContain('skip=0')
    expect(url).toContain('limit=100')
  })

  it('toggleAutoExecute 请求 /ext/channel_api/tasks/{id}', async () => {
    await longTermTaskApi.toggleAutoExecute('t1', true)

    expect(getMock).toHaveBeenCalledTimes(1)
    expect(getMock.mock.calls[0][0]).toBe('/ext/channel_api/tasks/t1')
    expect(patchMock).toHaveBeenCalledTimes(1)
    expect(patchMock.mock.calls[0][0]).toBe('/ext/channel_api/tasks/t1')
  })

  it('cancelLongTermTask 请求 POST /ext/channel_api/tasks/{id}/cancel', async () => {
    await longTermTaskApi.cancelLongTermTask('t1', '测试取消')

    expect(postMock).toHaveBeenCalledTimes(1)
    expect(postMock.mock.calls[0][0]).toBe('/ext/channel_api/tasks/t1/cancel')
  })

  it('deleteLongTermTask 请求 DELETE /ext/channel_api/tasks/{id}', async () => {
    await longTermTaskApi.deleteLongTermTask('t1')

    expect(deleteMock).toHaveBeenCalledTimes(1)
    expect(deleteMock.mock.calls[0][0]).toBe('/ext/channel_api/tasks/t1')
  })

  it('pause/resume 请求 PATCH /ext/channel_api/tasks/{id}', async () => {
    await longTermTaskApi.pauseLongTermTask('t1')
    await longTermTaskApi.resumeLongTermTask('t1')

    expect(patchMock).toHaveBeenCalledTimes(2)
    expect(patchMock.mock.calls[0][0]).toBe('/ext/channel_api/tasks/t1')
    expect(patchMock.mock.calls[1][0]).toBe('/ext/channel_api/tasks/t1')
  })
})
