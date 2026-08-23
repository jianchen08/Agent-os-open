/**
 * 调试中心 query 行为测试（query 化批次 3 核心验收）
 *
 * 验证「mount 即拉、零缓存」→ stale-while-revalidate 的两个契约：
 * 1. 二次挂载零请求：组件卸载重挂（切页往返）在 staleTime 窗口内 queryFn
 *    只执行一次（useDebugSessionsQuery / useLlmPayloadDiagQuery 两个页面级 hook）；
 * 2. 翻页/过滤显式重拉：静态 key 槽下 page/status 变化触发 refetch
 *    （useDebugTasksQuery），同一 staleTime 窗口内同参重挂仍零请求。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGetSessions = vi.fn()
const mockGetPayloadList = vi.fn()
const mockGetTaskList = vi.fn()

vi.mock('@/services/api/executionRecords', () => ({
  getExecutionRecordsSessions: mockGetSessions,
  getExecutionRecords: vi.fn(),
}))

vi.mock('@/services/api/llmPayload', () => ({
  getPayloadDiagList: mockGetPayloadList,
  getPayloadDiagFile: vi.fn(),
}))

vi.mock('@/services/api/monitoring', () => ({
  getTaskList: mockGetTaskList,
}))

describe('useDebugQueries（批次 3 验收）', () => {
  let queryClient: QueryClient
  let useDebugSessionsQuery: typeof import('../useDebugQueries')['useDebugSessionsQuery']
  let useLlmPayloadDiagQuery: typeof import('../useDebugQueries')['useLlmPayloadDiagQuery']
  let useDebugTasksQuery: typeof import('../useDebugQueries')['useDebugTasksQuery']

  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }

  beforeEach(async () => {
    vi.resetModules()
    mockGetSessions.mockReset()
    mockGetPayloadList.mockReset()
    mockGetTaskList.mockReset()
    ;({ queryClient } = await import('@/services/query/queryClient'))
    queryClient.clear()
    ;({ useDebugSessionsQuery, useLlmPayloadDiagQuery, useDebugTasksQuery } =
      await import('../useDebugQueries'))
  })

  it('useDebugSessionsQuery：卸载重挂（切页往返）在 staleTime 窗口内零重复请求', async () => {
    mockGetSessions.mockResolvedValue({ sessions: [{ id: 's1', title: '会话1', created_at: '2026-01-01T00:00:00', updated_at: '2026-01-01T00:00:00', record_count: 1 }], total: 1 })

    const first = renderHook(() => useDebugSessionsQuery(), { wrapper })
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true))
    expect(mockGetSessions).toHaveBeenCalledTimes(1)

    // 卸载（离开页面）→ 重新挂载（回到页面）：缓存仍新鲜，不发请求
    first.unmount()
    const second = renderHook(() => useDebugSessionsQuery(), { wrapper })
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true))
    expect(second.result.current.data?.total).toBe(1)
    expect(mockGetSessions).toHaveBeenCalledTimes(1)
  })

  it('useLlmPayloadDiagQuery：二次挂载零请求（页面级缓存复用）', async () => {
    mockGetPayloadList.mockResolvedValue({ items: [{ name: 'a.json', ts: 1, model: 'M', msgs_hash: 'h', msg_count: 1 }], total: 1 })

    const first = renderHook(() => useLlmPayloadDiagQuery(1), { wrapper })
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true))
    expect(mockGetPayloadList).toHaveBeenCalledTimes(1)

    first.unmount()
    const second = renderHook(() => useLlmPayloadDiagQuery(1), { wrapper })
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true))
    expect(mockGetPayloadList).toHaveBeenCalledTimes(1)
  })

  it('useDebugTasksQuery：翻页显式重拉；重挂同参零请求', async () => {
    mockGetTaskList.mockResolvedValue({ items: [], total: 0 })

    const hook = renderHook(({ page }) => useDebugTasksQuery({ page, pageSize: 20 }), {
      initialProps: { page: 1 },
      wrapper,
    })
    await waitFor(() => expect(hook.result.current.isSuccess).toBe(true))
    expect(mockGetTaskList).toHaveBeenCalledTimes(1)

    // 翻页 → 显式重拉
    hook.rerender({ page: 2 })
    await waitFor(() => expect(mockGetTaskList).toHaveBeenCalledTimes(2))

    // 卸载重挂同参：缓存命中，不再重复请求
    hook.unmount()
    const remount = renderHook(() => useDebugTasksQuery({ page: 2, pageSize: 20 }), { wrapper })
    await waitFor(() => expect(remount.result.current.isSuccess).toBe(true))
    expect(mockGetTaskList).toHaveBeenCalledTimes(2)
  })
})
