/**
 * useTaskPolling Hook 测试（真实 hook 行为）
 *
 * 通过 renderHook 挂载真实 useTaskPolling，fake timers 推进断言：
 * - 轮询按 interval 触发 store.fetchTasks
 * - 页面不可见时跳过本次 tick
 * - enabled=false 不启动
 * - 卸载后定时器清理（advanceTimersByTime 后不再 fetch），重复卸载安全
 *
 * 注：本 hook 是 WS 实时链路的兜底轮询——任务进入终态【不】停止轮询
 * （实时链路失效时仍需靠它恢复），不设「终态停止」断言；终态判断由
 * 导出的 isTerminalTask 纯函数覆盖。
 */

import { renderHook } from '@testing-library/react'
import { act } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useTaskPolling, isTerminalTask } from '../useTaskPolling'

// ---- Mocks ----

const mockFetchTasks = vi.fn().mockResolvedValue(undefined)

vi.mock('@/stores/longTermTaskStore', () => ({
  useLongTermTaskStore: {
    getState: () => ({
      tasks: [],
      isLoading: false,
      error: null,
      activeTaskId: null,
      fetchTasks: mockFetchTasks,
    }),
  },
}))

vi.mock('@/utils/logger', () => ({
  loggers: { taskPolling: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() } },
}))

// ---- Tests ----

describe('useTaskPolling - 真实 hook 轮询行为', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mockFetchTasks.mockClear()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('挂载后按 interval 触发 store.fetchTasks', () => {
    const { unmount } = renderHook(() => useTaskPolling({ interval: 3000 }))

    // 未到间隔不触发
    vi.advanceTimersByTime(2999)
    expect(mockFetchTasks).not.toHaveBeenCalled()

    // 3s → 第 1 次；再 3s → 第 2 次
    vi.advanceTimersByTime(1)
    expect(mockFetchTasks).toHaveBeenCalledTimes(1)
    vi.advanceTimersByTime(3000)
    expect(mockFetchTasks).toHaveBeenCalledTimes(2)

    unmount()
  })

  it('默认间隔 5000ms', () => {
    const { unmount } = renderHook(() => useTaskPolling())

    vi.advanceTimersByTime(4999)
    expect(mockFetchTasks).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(mockFetchTasks).toHaveBeenCalledTimes(1)

    unmount()
  })

  it('enabled=false 时不启动轮询', () => {
    const { unmount } = renderHook(() => useTaskPolling({ interval: 1000, enabled: false }))

    vi.advanceTimersByTime(10000)
    expect(mockFetchTasks).not.toHaveBeenCalled()

    unmount()
  })

  it('页面不可见时跳过本次 tick，恢复可见后继续', () => {
    const { unmount } = renderHook(() => useTaskPolling({ interval: 1000 }))

    // 隐藏：tick 被跳过
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    vi.advanceTimersByTime(3000)
    expect(mockFetchTasks).not.toHaveBeenCalled()

    // 恢复可见：下一个 tick 恢复轮询
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    vi.advanceTimersByTime(1000)
    expect(mockFetchTasks).toHaveBeenCalledTimes(1)

    unmount()
  })

  it('卸载后定时器清理：advanceTimersByTime 不再触发 fetch', () => {
    const { unmount } = renderHook(() => useTaskPolling({ interval: 1000 }))

    vi.advanceTimersByTime(2000)
    expect(mockFetchTasks).toHaveBeenCalledTimes(2)

    act(() => {
      unmount()
    })

    // 卸载后推进任意时长都不应再有 fetch（cleanup 清掉了 interval）
    vi.advanceTimersByTime(10000)
    expect(mockFetchTasks).toHaveBeenCalledTimes(2)
  })

  it('重复卸载安全（cleanup 幂等）', () => {
    const { unmount, rerender } = renderHook(({ enabled }: { enabled: boolean }) =>
      useTaskPolling({ interval: 1000, enabled }), { initialProps: { enabled: true } })

    // enabled 切换会重挂 effect（旧 cleanup 先跑）
    rerender({ enabled: false })
    act(() => {
      unmount()
    })
    // 二次 unmount（防御性）：不应抛异常
    act(() => {
      unmount()
    })

    vi.advanceTimersByTime(5000)
    expect(mockFetchTasks).not.toHaveBeenCalled()
  })
})

describe('isTerminalTask - 终态判断', () => {
  it('completed 是终态', () => expect(isTerminalTask('completed')).toBe(true))
  it('failed 是终态', () => expect(isTerminalTask('failed')).toBe(true))
  it('cancelled 是终态', () => expect(isTerminalTask('cancelled')).toBe(true))
  it('timeout 是终态', () => expect(isTerminalTask('timeout')).toBe(true))
  it('pending 不是终态', () => expect(isTerminalTask('pending')).toBe(false))
  it('running 不是终态', () => expect(isTerminalTask('running')).toBe(false))
  it('blocked 不是终态', () => expect(isTerminalTask('blocked')).toBe(false))
  it('suspended 不是终态', () => expect(isTerminalTask('suspended')).toBe(false))
})
