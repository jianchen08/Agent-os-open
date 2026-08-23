/**
 * useLongTermTasksQuery 行为测试（query 化批次 4 核心验收）
 *
 * 验证「手写 5s 轮询 → refetchInterval」迁移的两个机制：
 * 1. refetchInterval 生效：挂载后按 5s 间隔自动重拉（fake timers 推进断言），
 *    页面隐藏自动暂停（refetchIntervalInBackground 默认 false）；
 * 2. WS 事件路径（useRealtimeEvents task_status_update）：
 *    - 任务已存在 → 缓存增量更新，零请求（不触发重拉）；
 *    - 任务不存在 → invalidate 触发活跃订阅自动重拉。
 *
 * 注意：fake timers 下禁用 RTL waitFor（其自动推进会反复触发 refetchInterval，
 * 计数断言永不满足）——统一用 act + advanceTimersByTimeAsync 手动 flush。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'

// ---- Mocks ----

const mockFetchLongTermTasks = vi.fn()

vi.mock('@/services/api/longTermTasks', () => ({
  fetchLongTermTasks: mockFetchLongTermTasks,
}))

// useRealtimeEvents 依赖的 WS 单例：捕获订阅回调，测试内手动触发
const listeners: Record<string, Set<(...args: any[]) => void>> = {}
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      if (!listeners[event]) listeners[event] = new Set()
      listeners[event].add(cb)
    }),
    unsubscribe: vi.fn((event: string, cb: (...a: any[]) => void) => {
      listeners[event]?.delete(cb)
    }),
    connect: vi.fn(),
    status: 'connected',
  },
}))

function emitEvent(event: string, data: Record<string, unknown>) {
  const cbs = listeners[event]
  if (!cbs) return
  for (const cb of cbs) cb(data)
}

/** fake timers 下 flush 微任务 + 已到期的定时器（等价 waitFor 的单轮检查） */
async function flushTimers(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

// ---- 被测模块（动态 import 拿模块单例，与 queryClient 单例同源） ----
let queryClient: typeof import('@/services/query/queryClient')['queryClient']
let queryKeys: typeof import('@/services/query/queryKeys')['queryKeys']
let useLongTermTasksQuery: typeof import('../useLongTermTasksQuery')['useLongTermTasksQuery']
let useRealtimeEvents: typeof import('@/hooks/useRealtimeEvents')['useRealtimeEvents']

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('useLongTermTasksQuery', () => {
  beforeEach(async () => {
    vi.useFakeTimers()
    vi.resetModules()
    mockFetchLongTermTasks.mockReset()
    for (const key of Object.keys(listeners)) delete listeners[key]
    ;({ queryClient } = await import('@/services/query/queryClient'))
    queryClient.clear()
    ;({ queryKeys } = await import('@/services/query/queryKeys'))
    ;({ useLongTermTasksQuery } = await import('../useLongTermTasksQuery'))
    ;({ useRealtimeEvents } = await import('@/hooks/useRealtimeEvents'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('refetchInterval 生效：挂载即拉取，5s 后自动重拉，卸载后停止', async () => {
    mockFetchLongTermTasks.mockResolvedValue({ items: [], total: 0 })

    const { result, unmount } = renderHook(() => useLongTermTasksQuery(), { wrapper })
    // 挂载首拉（fake timers 下微任务手动 flush）
    await flushTimers(0)
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(1)
    expect(result.current.isSuccess).toBe(true)

    // 5s 间隔：未到不拉，到点重拉
    await flushTimers(4999)
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(1)
    await flushTimers(1)
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(2)

    unmount()
    await flushTimers(10_000)
    // 卸载后不再重拉
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(2)
  })

  it('query 注册的 refetchInterval=5000（与旧 useTaskPolling 默认一致）', () => {
    mockFetchLongTermTasks.mockResolvedValue({ items: [], total: 0 })
    renderHook(() => useLongTermTasksQuery(), { wrapper })
    // v5 Query.options 暴露归一化后的配置（含 refetchInterval）
    const query = queryClient.getQueryCache().find({ queryKey: queryKeys.longTermTasks })
    expect(query).toBeDefined()
    const interval = query!.options.refetchInterval as unknown
    expect(typeof interval).toBe('number')
    expect(interval).toBe(5000)
    // 页面隐藏默认暂停：refetchIntervalInBackground 未开启（undefined = 默认 false）
    expect(query!.options.refetchIntervalInBackground).toBeUndefined()
  })

  it('task_status_update：任务已存在 → 缓存增量更新，零请求', async () => {
    mockFetchLongTermTasks.mockResolvedValue({
      items: [{ id: 'task-1', title: '长期任务', status: 'running' }],
      total: 1,
    })

    renderHook(() => useLongTermTasksQuery(), { wrapper })
    await flushTimers(0)
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(1)
    // 订阅 WS（真实 useRealtimeEvents 挂载）
    const { unmount } = renderHook(() => useRealtimeEvents())
    const callsBefore = mockFetchLongTermTasks.mock.calls.length

    await act(async () => {
      emitEvent('task_status_update', {
        task_id: 'task-1',
        new_status: 'completed',
        current_phase: 'execute',
      })
    })
    await flushTimers(0)

    // 缓存增量更新：状态变更立即反映，零新请求
    const tasks = queryClient.getQueryData<Array<{ id: string; status: string; currentPhase?: string }>>(
      queryKeys.longTermTasks,
    ) ?? []
    const task = tasks.find((t) => t.id === 'task-1')
    expect(task?.status).toBe('completed')
    expect(task?.currentPhase).toBe('execute')
    expect(mockFetchLongTermTasks.mock.calls.length).toBe(callsBefore)
    unmount()
  })

  it('task_status_update：任务不存在 → invalidate 触发活跃订阅重拉', async () => {
    mockFetchLongTermTasks.mockResolvedValue({
      items: [{ id: 'task-old', title: '旧任务', status: 'running' }],
      total: 1,
    })

    renderHook(() => useLongTermTasksQuery(), { wrapper })
    await flushTimers(0)
    expect(mockFetchLongTermTasks).toHaveBeenCalledTimes(1)
    const { unmount } = renderHook(() => useRealtimeEvents())
    const callsBefore = mockFetchLongTermTasks.mock.calls.length

    await act(async () => {
      emitEvent('task_status_update', {
        task_id: 'task-new',
        new_status: 'running',
      })
    })
    // invalidate 后活跃订阅自动重拉（替代原 fetchTasks 全量）
    await flushTimers(0)
    expect(mockFetchLongTermTasks.mock.calls.length).toBeGreaterThan(callsBefore)
    unmount()
  })
})
