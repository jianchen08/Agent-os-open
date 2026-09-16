// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * 管道轮询 hooks 可见性门控契约测试
 *
 * refetchInterval 参数语义：
 * - 缺省：30s 兜底轮询（旧行为不变，ChatContainer 等其他消费方不受影响）
 * - false（面板不可见）：冻结轮询，仅挂载拉取一次
 * 挂载即拉取的语义在两种取值下都保持。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useAllTasksQuery } from '../useAllTasksQuery'
import { usePipelineRunsQuery } from '../usePipelineRunsQuery'

const apiGet = vi.fn()
vi.mock('@/services/api/client', () => {
  // client.ts 同时有 default 与命名导出 apiClient（usePipelineRunsQuery 的
  // fetch 链路走命名导出），两者必须指向同一 get 以共享调用计数
  const client = Object.assign(vi.fn(), { get: (...args: unknown[]) => apiGet(...args) })
  return { default: client, apiClient: client }
})

function fetchCallCount() {
  return apiGet.mock.calls.length
}

async function renderWithClient(hook: () => unknown) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  const view = renderHook(hook as () => unknown, { wrapper })
  // 挂载即拉取完成（假定时器下以微任务冲洗代替 waitFor——waitFor 自身轮询
  // 定时器会被冻结导致挂起；多轮冲洗覆盖 fetch 包装层的 promise 链深度）
  await act(async () => {
    for (let i = 0; i < 8; i++) {
      await Promise.resolve()
    }
  })
  expect(fetchCallCount()).toBeGreaterThanOrEqual(1)
  return view
}

beforeEach(() => {
  vi.useFakeTimers()
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: { items: [] } })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('管道轮询可见性门控', () => {
  it('useAllTasksQuery 缺省 30s 轮询（旧行为）', async () => {
    await renderWithClient(() => useAllTasksQuery())
    const before = fetchCallCount()
    await vi.advanceTimersByTimeAsync(65_000)
    // 30s/60s 两个轮询点
    expect(fetchCallCount()).toBeGreaterThanOrEqual(before + 2)
  })

  it('useAllTasksQuery(false)：冻结轮询，仅挂载一次', async () => {
    await renderWithClient(() => useAllTasksQuery(false))
    const before = fetchCallCount()
    await vi.advanceTimersByTimeAsync(65_000)
    expect(fetchCallCount()).toBe(before)
  })

  it('usePipelineRunsQuery(false)：冻结轮询', async () => {
    await renderWithClient(() => usePipelineRunsQuery(false))
    const before = fetchCallCount()
    await vi.advanceTimersByTimeAsync(65_000)
    expect(fetchCallCount()).toBe(before)
  })
})
