/**
 * useSessionsQuery 行为测试（query 化批次 1 核心验收）
 *
 * 验证「每次进入页面不再重新加载」的两个机制：
 * 1. 组件卸载重挂（切页往返）：staleTime 窗口内 queryFn 只执行一次，零重复请求；
 * 2. 会话列表持久化缓存读写 helpers（readSessions/updateSessionsCache/ensureSessionsLoaded）
 *    的缓存命中语义（非组件流程零请求直读）。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGetSessions = vi.fn()

vi.mock('@/services/api/session', () => ({
  getSessions: mockGetSessions,
}))

function makeSession(id: string, title: string) {
  return {
    id,
    title,
    agentId: null,
    activePipelineId: `pipe-${id}`,
    pipelineIds: [`pipe-${id}`],
    starred: false,
    pinned: false,
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }
}

describe('useSessionsQuery', () => {
  let useSessionsQuery: typeof import('../useSessionsQuery')['useSessionsQuery']
  let readSessions: typeof import('../useSessionsQuery')['readSessions']
  let updateSessionsCache: typeof import('../useSessionsQuery')['updateSessionsCache']
  let ensureSessionsLoaded: typeof import('../useSessionsQuery')['ensureSessionsLoaded']
  let queryClient: typeof import('@/services/query/queryClient')['queryClient']
  let queryKeys: typeof import('@/services/query/queryKeys')['queryKeys']

  function wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  }

  beforeEach(async () => {
    vi.resetModules()
    mockGetSessions.mockReset()
    ;({ queryClient } = await import('@/services/query/queryClient'))
    queryClient.clear()
    ;({ useSessionsQuery, readSessions, updateSessionsCache, ensureSessionsLoaded } =
      await import('../useSessionsQuery'))
    ;({ queryKeys } = await import('@/services/query/queryKeys'))
  })

  it('挂载即拉取；卸载重挂（切页往返）在 staleTime 窗口内零重复请求', async () => {
    mockGetSessions.mockResolvedValue([makeSession('s1', '会话1')])

    const first = renderHook(() => useSessionsQuery(), { wrapper })
    await waitFor(() => expect(first.result.current.isSuccess).toBe(true))
    expect(mockGetSessions).toHaveBeenCalledTimes(1)

    // 卸载（离开页面）→ 重新挂载（回到页面）：缓存仍新鲜，不发请求
    first.unmount()
    const second = renderHook(() => useSessionsQuery(), { wrapper })
    await waitFor(() => expect(second.result.current.isSuccess).toBe(true))
    expect(second.result.current.data).toEqual([makeSession('s1', '会话1')])
    expect(mockGetSessions).toHaveBeenCalledTimes(1)
  })

  it('readSessions：无缓存返回空数组，有缓存直读（不触发请求）', async () => {
    expect(readSessions()).toEqual([])
    queryClient.setQueryData(queryKeys.sessions, [makeSession('s1', '缓存会话')])
    expect(readSessions().map((s) => s.title)).toEqual(['缓存会话'])
    expect(mockGetSessions).not.toHaveBeenCalled()
  })

  it('updateSessionsCache：乐观更新立即反映到 readSessions', () => {
    queryClient.setQueryData(queryKeys.sessions, [makeSession('s1', '原标题')])
    updateSessionsCache((prev) =>
      prev.map((s) => (s.id === 's1' ? { ...s, title: '新标题' } : s)),
    )
    expect(readSessions()[0].title).toBe('新标题')
  })

  it('ensureSessionsLoaded：缓存命中零请求直读；无缓存时拉取一次并写入', async () => {
    // 无缓存：拉取并写入
    mockGetSessions.mockResolvedValue([makeSession('s2', '冷数据')])
    const loaded = await ensureSessionsLoaded()
    expect(loaded.map((s) => s.id)).toEqual(['s2'])
    expect(mockGetSessions).toHaveBeenCalledTimes(1)

    // 再次调用：缓存命中，零新请求
    const again = await ensureSessionsLoaded()
    expect(again.map((s) => s.id)).toEqual(['s2'])
    expect(mockGetSessions).toHaveBeenCalledTimes(1)
  })
})
