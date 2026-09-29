// @feature: FP-T12 前端适配(插件宿主 query) | @ci: frontend-test
/**
 * usePluginHostsQuery / usePluginRuntimeQuery 行为测试（插件进程观测面）
 *
 * 验证轮询契约：挂载即拉、间隔自动重拉（hosts 10s 对齐内核采集周期；
 * runtime 30s 对齐原「插件运行」表声明周期）、卸载后停止。fake timers 下
 * 禁用 RTL waitFor（自动推进会反复触发 refetchInterval）——统一 act +
 * advanceTimersByTimeAsync 手动 flush（useLongTermTasksQuery 同法）。
 */

import { QueryClientProvider } from '@tanstack/react-query'
import { renderHook, act } from '@testing-library/react'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import type { QueryClient } from '@tanstack/react-query'
import type * as hostsMod from '../usePluginHostsQuery'
import type * as runtimeMod from '../usePluginRuntimeQuery'
import type { ReactNode } from 'react'

const mockGetPluginHosts = vi.fn()
const mockGetPluginRuntimeRows = vi.fn()

vi.mock('@/services/api/pluginHosts', () => ({
  getPluginHosts: mockGetPluginHosts,
  getPluginRuntimeRows: mockGetPluginRuntimeRows,
}))

/** fake timers 下 flush 微任务 + 已到期的定时器 */
async function flushTimers(ms = 0) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

let queryClient: QueryClient
let usePluginHostsQuery: hostsMod['usePluginHostsQuery']
let usePluginRuntimeQuery: runtimeMod['usePluginRuntimeQuery']

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}

describe('usePluginHostsQuery / usePluginRuntimeQuery', () => {
  beforeEach(async () => {
    vi.useFakeTimers()
    vi.resetModules()
    mockGetPluginHosts.mockReset()
    mockGetPluginRuntimeRows.mockReset()
    ;({ queryClient } = await import('@/services/query/queryClient'))
    queryClient.clear()
    ;({ usePluginHostsQuery } = await import('../usePluginHostsQuery'))
    ;({ usePluginRuntimeQuery } = await import('../usePluginRuntimeQuery'))
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('hosts：挂载即拉，10s 间隔自动重拉，卸载后停止', async () => {
    mockGetPluginHosts.mockResolvedValue({ hosts: [], pending_spawn: [] })

    const { result, unmount } = renderHook(() => usePluginHostsQuery(), { wrapper })
    await flushTimers(0)
    expect(mockGetPluginHosts).toHaveBeenCalledTimes(1)
    expect(result.current.isSuccess).toBe(true)

    // 10s 间隔：未到不拉，到点重拉
    await flushTimers(9_999)
    expect(mockGetPluginHosts).toHaveBeenCalledTimes(1)
    await flushTimers(1)
    expect(mockGetPluginHosts).toHaveBeenCalledTimes(2)

    unmount()
    await flushTimers(10_000)
    expect(mockGetPluginHosts).toHaveBeenCalledTimes(2)
  })

  it('runtime：挂载即拉，30s 间隔自动重拉（对齐原运行表周期），卸载后停止', async () => {
    mockGetPluginRuntimeRows.mockResolvedValue([])

    const { result, unmount } = renderHook(() => usePluginRuntimeQuery(), { wrapper })
    await flushTimers(0)
    expect(mockGetPluginRuntimeRows).toHaveBeenCalledTimes(1)
    expect(result.current.isSuccess).toBe(true)

    // 30s 间隔：29.9s 不拉，到点重拉
    await flushTimers(29_999)
    expect(mockGetPluginRuntimeRows).toHaveBeenCalledTimes(1)
    await flushTimers(1)
    expect(mockGetPluginRuntimeRows).toHaveBeenCalledTimes(2)

    unmount()
    await flushTimers(30_000)
    expect(mockGetPluginRuntimeRows).toHaveBeenCalledTimes(2)
  })
})
