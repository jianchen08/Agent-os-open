// @feature: FP-T12 useCostControl 补测 | @ci: frontend-test
/**
 * useCostControl / useBudgetStatus 补测：
 * - 共享取数骨架的状态机（loading/error/结果落点/异常透传）
 * - 五个取数包装与 resetBudget 后刷新
 * - WS cost_update 事件驱动刷新与卸载退订、回调内失败不上抛
 * - useBudgetStatus 自动拉取/手动 refetch/params 最新值/失败兜底文案
 *
 * mock 仅外部依赖（HTTP API / WS 单例），断言可观察状态与调用效果。
 */

import { act, renderHook } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'

const {
  getBudgetStatus,
  getUsageStatistics,
  getCostConfig,
  getCostReport,
  resetBudget,
} = vi.hoisted(() => ({
  getBudgetStatus: vi.fn(),
  getUsageStatistics: vi.fn(),
  getCostConfig: vi.fn(),
  getCostReport: vi.fn(),
  resetBudget: vi.fn(),
}))

vi.mock('@/services/api/costControl', () => ({
  getBudgetStatus,
  getUsageStatistics,
  getCostConfig,
  getCostReport,
  resetBudget,
}))

const { wsSubscribe, wsUnsubscribe } = vi.hoisted(() => ({
  wsSubscribe: vi.fn(),
  wsUnsubscribe: vi.fn(),
}))

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { subscribe: wsSubscribe, unsubscribe: wsUnsubscribe },
}))

import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { useBudgetStatus, useCostControl } from '../useCostControl'
import type {
  BudgetStatusResponse,
  CostConfigResponse,
  CostReportResponse,
  UsageStatisticsResponse,
} from '@/services/api/costControl'

const budgetFixture: BudgetStatusResponse = {
  scope: 'global',
  limit: 1000,
  used: 250,
  remaining: 750,
  usage_percent: 25,
  alert_level: 'normal',
  estimated_cost: 0.25,
}

const usageFixture: UsageStatisticsResponse = {
  global_stats: {
    daily_tokens: 100,
    monthly_tokens: 2000,
    daily_limit: 5000,
    monthly_limit: 50000,
    daily_usage_percent: 2,
    monthly_usage_percent: 4,
    estimated_daily_cost: 0.01,
    estimated_monthly_cost: 0.2,
  },
  tasks: [],
  sessions: [],
  recent_records: [],
  updated_at: '2026-09-13T00:00:00Z',
}

const configFixture: CostConfigResponse = {
  daily_token_limit: 5000,
  monthly_token_limit: 50000,
  per_task_token_limit: 1000,
  per_session_token_limit: 2000,
  warning_threshold: 0.8,
  critical_threshold: 0.95,
  auto_save_at_warning: true,
  auto_pause_at_critical: true,
  auto_stop_at_exhausted: false,
}

const reportFixture: CostReportResponse = {
  period: 'daily',
  start_date: '2026-09-12',
  end_date: '2026-09-13',
  total_tokens: 300,
  total_cost: 0.03,
  by_model: {},
  by_task: {},
  daily_breakdown: [],
}

/** 挂载 useCostControl 并等待挂载期 refreshAll 完成 */
async function mountCostControl() {
  const rendered = renderHook(() => useCostControl())
  await act(async () => {})
  return rendered
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('useCostControl — 挂载自动刷新', () => {
  it('挂载即并发拉取预算/统计/配置并落入对应 state，结束回到非 loading', async () => {
    getBudgetStatus.mockResolvedValue(budgetFixture)
    getUsageStatistics.mockResolvedValue(usageFixture)
    getCostConfig.mockResolvedValue(configFixture)

    const { result } = await mountCostControl()

    expect(getBudgetStatus).toHaveBeenCalledTimes(1)
    expect(getUsageStatistics).toHaveBeenCalledTimes(1)
    expect(getCostConfig).toHaveBeenCalledTimes(1)
    expect(result.current.budgetStatus).toEqual(budgetFixture)
    expect(result.current.usageStats).toEqual(usageFixture)
    expect(result.current.costConfig).toEqual(configFixture)
    expect(result.current.isLoading).toBe(false)
    expect(result.current.error).toBeNull()
  })
})

describe('useCostControl — 手动取数包装', () => {
  beforeEach(() => {
    getBudgetStatus.mockResolvedValue(budgetFixture)
    getUsageStatistics.mockResolvedValue(usageFixture)
    getCostConfig.mockResolvedValue(configFixture)
    getCostReport.mockResolvedValue(reportFixture)
    resetBudget.mockResolvedValue({ message: 'ok' })
  })

  it('fetchCostReport 透传 period 参数并落入 costReport state', async () => {
    const { result } = await mountCostControl()

    await act(async () => {
      const report = await result.current.fetchCostReport({ period: 'weekly' })
      expect(report).toEqual(reportFixture)
    })

    expect(getCostReport).toHaveBeenCalledWith({ period: 'weekly' })
    expect(result.current.costReport).toEqual(reportFixture)
  })

  it('fetchBudgetStatus 失败（Error）：error 落 err.message、异常向调用方透传', async () => {
    const { result } = await mountCostControl()
    getBudgetStatus.mockRejectedValueOnce(new Error('预算服务不可用'))

    let caught: unknown
    await act(async () => {
      try {
        await result.current.fetchBudgetStatus()
      } catch (err) {
        caught = err
      }
    })

    expect(caught).toBeInstanceOf(Error)
    expect((caught as Error).message).toBe('预算服务不可用')
    expect(result.current.error).toBe('预算服务不可用')
    expect(result.current.isLoading).toBe(false)
  })

  it('fetchUsageStatistics 失败（非 Error 值）：error 落兜底文案', async () => {
    const { result } = await mountCostControl()
    getUsageStatistics.mockRejectedValueOnce('network-down')

    let caught: unknown
    await act(async () => {
      try {
        await result.current.fetchUsageStatistics()
      } catch (err) {
        caught = err
      }
    })

    expect(caught).toBe('network-down')
    expect(result.current.error).toBe('获取使用统计失败')
  })

  it('resetBudget：重置成功后以同参数刷新预算状态', async () => {
    const { result } = await mountCostControl()
    const callsBefore = getBudgetStatus.mock.calls.length

    await act(async () => {
      const res = await result.current.resetBudget({ task_id: 'task-9' })
      expect(res).toEqual({ message: 'ok' })
    })

    expect(resetBudget).toHaveBeenCalledWith({ task_id: 'task-9' })
    expect(getBudgetStatus.mock.calls.length).toBeGreaterThan(callsBefore)
    expect(getBudgetStatus).toHaveBeenLastCalledWith({ task_id: 'task-9' })
    expect(result.current.error).toBeNull()
  })
})

describe('useCostControl — WS cost_update 事件驱动刷新', () => {
  beforeEach(() => {
    getBudgetStatus.mockResolvedValue(budgetFixture)
    getUsageStatistics.mockResolvedValue(usageFixture)
    getCostConfig.mockResolvedValue(configFixture)
  })

  it('订阅 COST_UPDATE；事件到达重新拉取使用统计；卸载退订同一 handler', async () => {
    const { result, unmount } = await mountCostControl()

    expect(wsSubscribe).toHaveBeenCalledWith(
      WS_SERVER_EVENTS.COST_UPDATE,
      expect.any(Function),
    )
    expect(getUsageStatistics).toHaveBeenCalledTimes(1)

    const handler = wsSubscribe.mock.calls.find(
      ([event]) => event === WS_SERVER_EVENTS.COST_UPDATE,
    )?.[1] as () => void

    await act(async () => {
      handler()
    })
    expect(getUsageStatistics).toHaveBeenCalledTimes(2)
    expect(result.current.usageStats).toEqual(usageFixture)

    unmount()
    expect(wsUnsubscribe).toHaveBeenCalledWith(
      WS_SERVER_EVENTS.COST_UPDATE,
      handler,
    )
  })

  it('事件触发时拉取失败：失败态由 error state 承载，回调不上抛（无 unhandled rejection）', async () => {
    const { result } = await mountCostControl()
    getUsageStatistics.mockRejectedValueOnce(new Error('统计接口 500'))

    const handler = wsSubscribe.mock.calls.find(
      ([event]) => event === WS_SERVER_EVENTS.COST_UPDATE,
    )?.[1] as () => void

    await expect(
      act(async () => {
        handler()
      }),
    ).resolves.toBeUndefined()

    expect(result.current.error).toBe('统计接口 500')
  })
})

describe('useBudgetStatus', () => {
  beforeEach(() => {
    getBudgetStatus.mockResolvedValue(budgetFixture)
  })

  it('默认自动拉取一次并落 state', async () => {
    const { result } = renderHook(() => useBudgetStatus())
    await act(async () => {})

    expect(getBudgetStatus).toHaveBeenCalledTimes(1)
    expect(result.current.budgetStatus).toEqual(budgetFixture)
    expect(result.current.error).toBeNull()
  })

  it('autoFetch=false 不自动拉取，refetch 手动触发并携带最新 params', async () => {
    const { result, rerender } = renderHook(
      ({ params }: { params?: { task_id?: string } }) => useBudgetStatus(params, false),
      { initialProps: { params: { task_id: 't-1' } as { task_id?: string } } },
    )
    await act(async () => {})
    expect(getBudgetStatus).not.toHaveBeenCalled()

    rerender({ params: { task_id: 't-2' } })
    await act(async () => {
      const status = await result.current.refetch()
      expect(status).toEqual(budgetFixture)
    })

    expect(getBudgetStatus).toHaveBeenCalledTimes(1)
    expect(getBudgetStatus).toHaveBeenLastCalledWith({ task_id: 't-2' })
    expect(result.current.budgetStatus).toEqual(budgetFixture)
  })

  it('挂载自动拉取失败：错误落 state，被动拉取吞掉 rejection（不产生 unhandled rejection）', async () => {
    getBudgetStatus.mockRejectedValue(new Error('挂载期失败'))
    const { result } = renderHook(() => useBudgetStatus())
    await act(async () => {})

    expect(result.current.error).toBe('挂载期失败')
    expect(result.current.budgetStatus).toBeNull()
    expect(result.current.isLoading).toBe(false)
  })

  it('拉取失败（Error）：error 落 err.message 且 refetch 透传异常', async () => {
    const { result } = renderHook(() => useBudgetStatus())
    await act(async () => {})
    getBudgetStatus.mockRejectedValueOnce(new Error('配额查询失败'))

    let caught: unknown
    await act(async () => {
      try {
        await result.current.refetch()
      } catch (err) {
        caught = err
      }
    })

    expect((caught as Error).message).toBe('配额查询失败')
    expect(result.current.error).toBe('配额查询失败')
  })

  it('拉取失败（非 Error 值）：error 落兜底文案「获取预算状态失败」', async () => {
    const { result } = renderHook(() => useBudgetStatus())
    await act(async () => {})
    getBudgetStatus.mockRejectedValueOnce(42)

    let caught: unknown
    await act(async () => {
      try {
        await result.current.refetch()
      } catch (err) {
        caught = err
      }
    })

    expect(caught).toBe(42)
    expect(result.current.error).toBe('获取预算状态失败')
  })
})
