/** 成本控制 Hook 提供成本控制相关的 React Hook */

import { useState, useEffect, useCallback, useRef } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import {
  getBudgetStatus,
  getUsageStatistics,
  getCostConfig,
  getCostReport,
  resetBudget,
} from '@/services/api/costControl'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import type {
  BudgetStatusResponse,
  UsageStatisticsResponse,
  CostConfigResponse,
  CostReportResponse,
} from '@/services/api/costControl'

/** 使用成本控制 Hook */
export function useCostControl() {
  const [budgetStatus, setBudgetStatus] = useState<BudgetStatusResponse | null>(null)
  const [usageStats, setUsageStats] = useState<UsageStatisticsResponse | null>(null)
  const [costConfig, setCostConfig] = useState<CostConfigResponse | null>(null)
  const [costReport, setCostReport] = useState<CostReportResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 共享取数骨架：loading/error 状态机 + 异常透传。
   * 五个 fetch 差异点只有 API 调用、结果落点和失败文案——
   * 统一在此，消费侧各自薄包装（消除复制漂移）。
   */
  const run = useCallback(
    async <T>(
      fallbackMessage: string,
      action: () => Promise<T>,
      apply?: (data: T) => void,
    ): Promise<T> => {
      setIsLoading(true)
      setError(null)
      try {
        const data = await action()
        apply?.(data)
        return data
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : fallbackMessage
        setError(message)
        throw err
      } finally {
        setIsLoading(false)
      }
    },
    [],
  )

  /** 获取预算状态 */
  const fetchBudgetStatus = useCallback(
    (params?: { task_id?: string; session_id?: string }) =>
      run('获取预算状态失败', () => getBudgetStatus(params), setBudgetStatus),
    [run],
  )

  /** 获取使用统计 */
  const fetchUsageStatistics = useCallback(
    () => run('获取使用统计失败', getUsageStatistics, setUsageStats),
    [run],
  )

  /** 获取成本配置 */
  const fetchCostConfig = useCallback(
    () => run('获取成本配置失败', getCostConfig, setCostConfig),
    [run],
  )

  /** 获取成本报表 */
  const fetchCostReport = useCallback(
    (params?: { period?: 'daily' | 'weekly' | 'monthly' }) =>
      run('获取成本报表失败', () => getCostReport(params), setCostReport),
    [run],
  )

  /** 重置预算 */
  const resetBudgetData = useCallback(
    async (params?: { task_id?: string; session_id?: string }) =>
      run('重置预算失败', async () => {
        const result = await resetBudget(params)
        // 重置后刷新预算状态
        await fetchBudgetStatus(params)
        return result
      }),
    [run, fetchBudgetStatus],
  )

  /** 刷新所有成本控制数据 */
  const refreshAll = useCallback(
    async () =>
      run('刷新数据失败', async () => {
        await Promise.all([fetchBudgetStatus(), fetchUsageStatistics(), fetchCostConfig()])
      }),
    [run, fetchBudgetStatus, fetchUsageStatistics, fetchCostConfig],
  )

  // 初始化时加载数据
  useEffect(() => {
    refreshAll()
  }, [refreshAll])

  // 监听 WS cost_update 事件，事件驱动刷新使用统计
  useEffect(() => {
    const handleCostUpdate = () => {
      fetchUsageStatistics()
    }
    globalWS.subscribe(WS_SERVER_EVENTS.COST_UPDATE, handleCostUpdate)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.COST_UPDATE, handleCostUpdate)
    }
  }, [fetchUsageStatistics])

  return {
    // 数据
    budgetStatus,
    usageStats,
    costConfig,
    costReport,
    // 状态
    isLoading,
    error,
    // 方法
    fetchBudgetStatus,
    fetchUsageStatistics,
    fetchCostConfig,
    fetchCostReport,
    resetBudget: resetBudgetData,
    refreshAll,
  }
}

/** 使用预算状态 Hook */
export function useBudgetStatus(
  params?: { task_id?: string; session_id?: string },
  autoFetch = true,
) {
  const [budgetStatus, setBudgetStatus] = useState<BudgetStatusResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const paramsRef = useRef(params)
  useEffect(() => { paramsRef.current = params }, [params])

  /** 获取预算状态 */
  const fetchBudgetStatus = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const status = await getBudgetStatus(paramsRef.current)
      setBudgetStatus(status)
      return status
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取预算状态失败'
      setError(message)
      throw err
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (autoFetch) {
      // 自动拉取属被动获取：失败已记入 error state，不再向上抛
      // （挂载于常驻布局时无人 await，裸抛会成为未处理 rejection）
      fetchBudgetStatus().catch(() => undefined)
    }
  }, [autoFetch, fetchBudgetStatus])

  return {
    budgetStatus,
    isLoading,
    error,
    refetch: fetchBudgetStatus,
  }
}
