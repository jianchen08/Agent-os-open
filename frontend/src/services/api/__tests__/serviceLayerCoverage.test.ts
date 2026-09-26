// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 覆盖率收口批：服务层小模块（监控 API / 管道诊断 API / WS 票据 /
 * 工作空间读接口 / 会话编辑载荷）
 *
 * 断言可观察契约：请求 URL/参数/载荷形状与返回值解包（含缺省字段兜底、
 * 缺 ticket 抛错），不断言内部实现细节。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('../client', () => {
  const mockClient = {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return { default: mockClient, apiClient: mockClient }
})

import apiClient from '@/services/api/client'
import { getTaskList } from '@/services/api/monitoring'
import {
  getPipelineStateFull,
  getPipelineTraces,
} from '@/services/api/pipelineDiagnostics'
import { fetchWsTicket } from '@/services/auth/wsTicket'
import {
  getWorkspace,
  getWorkspaceArtifacts,
  getWorkspaceFileContent,
  getWorkspaceFileTree,
} from '@/services/api/workspaces'
import { WORKSPACE_SERVICE_ENDPOINTS, MONITORING_ENDPOINTS  } from '@/services/api/endpoints.generated'


const okResponse = (data: unknown) => ({ data })

// mockReset（非 clearAllMocks）：mockResolvedValueOnce 的一次性队列由 clear 不排空，
// 前一用例的残留会让后一用例先吃掉旧响应。
beforeEach(() => {
  vi.resetAllMocks()
})

afterEach(() => {
  vi.resetAllMocks()
})

describe('监控 API - getTaskList', () => {

  it('不传 status → 查询参数只带分页，items/total 原样透出', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ items: [{ id: 't1' }], total: 1 }),
    )

    const res = await getTaskList(2, 50)

    expect(apiClient.get).toHaveBeenCalledWith(
      MONITORING_ENDPOINTS.mon_tasks,
      { params: { page: 2, page_size: 50 } },
    )
    expect(res).toEqual({ items: [{ id: 't1' }], total: 1 })
  })

  it('传 status → 查询参数并入 status 键', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ items: [], total: 0 }))

    await getTaskList(1, 20, 'running')

    const [, cfg] = vi.mocked(apiClient.get).mock.calls[0]
    expect((cfg as { params: Record<string, unknown> }).params).toEqual({
      page: 1,
      page_size: 20,
      status: 'running',
    })
  })

  it('响应缺 items/total → 兜底空数组与 0（不抛错）', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

    const res = await getTaskList()

    // 缺省分页参数按签名默认值 1/20 落地
    expect(apiClient.get).toHaveBeenCalledWith(
      MONITORING_ENDPOINTS.mon_tasks,
      { params: { page: 1, page_size: 20 } },
    )
    expect(res).toEqual({ items: [], total: 0 })
  })

  it('items 为 null → 兜底空数组（?? 覆盖 null 分支）', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ items: null, total: null }),
    )

    expect(await getTaskList()).toEqual({ items: [], total: 0 })
  })

  it('网络失败可重试：开启 retry 后首次 500 重试成功', async () => {
    vi.mocked(apiClient.get)
      .mockRejectedValueOnce({ response: { status: 500 } })
      .mockResolvedValueOnce(okResponse({ items: [{ id: 'ok' }], total: 1 }))

    const res = await getTaskList(1, 20, undefined, {
      retry: true,
      maxRetries: 2,
      retryDelay: 1,
    })

    expect(res.items).toEqual([{ id: 'ok' }])
    expect(apiClient.get).toHaveBeenCalledTimes(2)
  })

  it('默认不开启重试：500 直接抛出（只调用一次）', async () => {
    vi.mocked(apiClient.get).mockRejectedValueOnce({ response: { status: 500 } })

    await expect(getTaskList()).rejects.toEqual({ response: { status: 500 } })
    expect(apiClient.get).toHaveBeenCalledTimes(1)
  })
})

describe('管道诊断 API', () => {

  it('getPipelineTraces 透传 pipeline_id 与 limit，返回响应体原文', async () => {
    const payload = { traces: [{ trace_id: 'x' }], total: 1, pipeline_id: 'p1' }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(payload))

    const res = await getPipelineTraces({ pipeline_id: 'p1', limit: 50 })

    expect(apiClient.get).toHaveBeenCalledWith(
      MONITORING_ENDPOINTS.mon_traces_by_pipeline,
      { params: { pipeline_id: 'p1', limit: 50 } },
    )
    expect(res).toEqual(payload)
  })

  it('getPipelineTraces 省略 limit → 参数中不含该键', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ traces: [], total: 0, pipeline_id: 'p2' }),
    )

    await getPipelineTraces({ pipeline_id: 'p2' })

    const [, cfg] = vi.mocked(apiClient.get).mock.calls[0]
    expect((cfg as { params: Record<string, unknown> }).params).toEqual({
      pipeline_id: 'p2',
    })
  })

  it('getPipelineStateFull 按管道 ID 拉取全字段视图', async () => {
    const payload = {
      pipeline_id: 'p1',
      fields: [{ field_key: 'task.status', field_value: 'running', updated_at: null }],
      runs: [{ run_id: 'r1' }],
      summary: null,
    }
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(payload))

    const res = await getPipelineStateFull('p1')

    expect(apiClient.get).toHaveBeenCalledWith(
      MONITORING_ENDPOINTS.mon_pipeline_state_full,
      { params: { pipeline_id: 'p1' } },
    )
    expect(res).toEqual(payload)
  })
})

describe('WS 握手票据 fetchWsTicket', () => {
  it('签发成功 → POST 票据端点并返回 ticket 字符串', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce(
      okResponse({ ticket: 'abc123', expires_in: 60 }),
    )

    await expect(fetchWsTicket()).resolves.toBe('abc123')
    expect(apiClient.post).toHaveBeenCalledWith('/api/v1/ws-ticket')
  })

  it('响应缺 ticket → 抛出契约错误（不返回 undefined）', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({}))

    await expect(fetchWsTicket()).rejects.toThrow('WS 票据响应缺少 ticket 字段')
  })

  it('ticket 为空串 → 同样按缺字段处理（falsy 判定）', async () => {
    vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse({ ticket: '' }))

    await expect(fetchWsTicket()).rejects.toThrow('WS 票据响应缺少 ticket 字段')
  })
})

describe('工作空间读接口', () => {
  it('getWorkspace 按 container_task_id 替换路径模板并透出载荷', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ id: 'w1' }))

    const res = await getWorkspace('t1')

    expect(apiClient.get).toHaveBeenCalledWith(
      WORKSPACE_SERVICE_ENDPOINTS.workspaces_get.replace('{container_task_id}', 't1'),
    )
    expect(res).toEqual({ id: 'w1' })
  })

  it('getWorkspaceFileTree 有树 → 原样返回', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ tree: [{ name: 'a.txt', type: 'file' }] }),
    )

    const res = await getWorkspaceFileTree('t2')

    expect(res.tree).toEqual([{ name: 'a.txt', type: 'file' }])
  })

  it('getWorkspaceFileTree 响应缺 tree → 兜底空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

    expect(await getWorkspaceFileTree('t2')).toEqual({ tree: [] })
  })

  it('getWorkspaceArtifacts 有制品 → 原样返回', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ items: [{ id: 'a1' }] }))

    expect(await getWorkspaceArtifacts('t3')).toEqual({ items: [{ id: 'a1' }] })
  })

  it('getWorkspaceArtifacts 响应缺 items → 兜底空数组', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({}))

    expect(await getWorkspaceArtifacts('t3')).toEqual({ items: [] })
  })

  it('getWorkspaceFileContent 以 query 参数带路径取文件内容', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ success: true, content: '<h1>hi</h1>' }),
    )

    const res = await getWorkspaceFileContent('t4', 'src/index.html')

    expect(apiClient.get).toHaveBeenCalledWith(
      WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace(
        '{container_task_id}',
        't4',
      ),
      { params: { path: 'src/index.html' } },
    )
    expect(res).toEqual({ success: true, content: '<h1>hi</h1>' })
  })

  it('getWorkspaceFileContent 失败响应原样透出（success=false + message）', async () => {
    vi.mocked(apiClient.get).mockResolvedValueOnce(
      okResponse({ success: false, message: '文件不存在' }),
    )

    const res = await getWorkspaceFileContent('t4', 'nope.txt')

    expect(res.success).toBe(false)
    expect(res.message).toBe('文件不存在')
  })
})
