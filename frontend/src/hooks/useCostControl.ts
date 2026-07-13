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

  /** 获取预算状态 */
  const fetchBudgetStatus = useCallback(
    async (params?: { task_id?: string; session_id?: string }) => {
      setIsLoading(true)
      setError(null)
      try {
        const status = await getBudgetStatus(params)
        setBudgetStatus(status)
        return status
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : '获取预算状态失败'
        setError(message)
        throw err
      } finally {
        setIsLoading(false)
      }
    },
    [],
  )

  /** 获取使用统计 */
  const fetchUsageStatistics = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const stats = await getUsageStatistics()
      setUsageStats(stats)
      return stats
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取使用统计失败'
      setError(message)
      throw err
    } finally {
      setIsLoading(false)
    }
  }, [])

  /** 获取成本配置 */
  const fetchCostConfig = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const config = await getCostConfig()
      setCostConfig(config)
      return config
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取成本配置失败'
      setError(message)
      throw err
    } finally {
      setIsLoading(false)
    }
  }, [])

  /** 获取成本报表 */
  const fetchCostReport = useCallback(
    async (params?: { period?: 'daily' | 'weekly' | 'monthly' }) => {
      setIsLoading(true)
      setError(null)
      try {
        const report = await getCostReport(params)
        setCostReport(report)
        return report
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : '获取成本报表失败'
        setError(message)
        throw err
      } finally {
        setIsLoading(false)
      }
    },
    [],
  )

  /** 重置预算 */
  const resetBudgetData = useCallback(
    async (params?: { task_id?: string; session_id?: string }) => {
      setIsLoading(true)
      setError(null)
      try {
        const result = await resetBudget(params)
        // 重置后刷新预算状态
        await fetchBudgetStatus(params)
        return result
      } catch (err: unknown) {
        const message = err instanceof Error ? err.message : '重置预算失败'
        setError(message)
        throw err
      } finally {
        setIsLoading(false)
      }
    },
    [fetchBudgetStatus],
  )

  /** 刷新所有成本控制数据 */
  const refreshAll = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      await Promise.all([fetchBudgetStatus(), fetchUsageStatistics(), fetchCostConfig()])
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '刷新数据失败'
      setError(message)
      throw err
    } finally {
      setIsLoading(false)
    }
  }, [fetchBudgetStatus, fetchUsageStatistics, fetchCostConfig])

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
      fetchBudgetStatus()
    }
  }, [autoFetch, fetchBudgetStatus])

  return {
    budgetStatus,
    isLoading,
    error,
    refetch: fetchBudgetStatus,
  }
}

/** 使用使用统计 Hook */
export function useUsageStatistics(autoFetch = true, refreshInterval = 60000) {
  const [usageStats, setUsageStats] = useState<UsageStatisticsResponse | null>(null)
  const [isLoading, setIsLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /** 获取使用统计 */
  const fetchUsageStatistics = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const stats = await getUsageStatistics()
      setUsageStats(stats)
      return stats
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取使用统计失败'
      setError(message)
      throw err
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    if (autoFetch) {
      fetchUsageStatistics()
    }
  }, [autoFetch, fetchUsageStatistics])

  // 监听 WS cost_update 事件，事件驱动刷新
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
    usageStats,
    isLoading,
    error,
    refetch: fetchUsageStatistics,
  }
}
