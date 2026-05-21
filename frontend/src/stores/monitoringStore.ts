/**
 * 监控页面状态管理 Store
 *
 * 使用真实后端 API 加载监控数据，支持自动刷新
 */

import { create } from 'zustand'
import * as monitoringApi from '@/services/api/monitoring'
import { getSessionTotalTokenUsage } from '@/services/api/sessions'
import { ErrorType, reportError } from '@/services/errorReporting'
import type { SessionTokenUsageResponse } from '@/services/api/sessions'
import type { CacheStats, SystemMetrics, TaskInfo, TaskStatistics, TokenUsage } from '@/types/monitoring'

/**
 * 监控状态接口
 */
interface MonitoringState {
  /** 系统性能指标 */
  metrics: SystemMetrics | null
  /** 任务执行统计 */
  statistics: TaskStatistics | null
  /** 最近任务列表 */
  recentTasks: TaskInfo[]
  /** Token 用量统计 */
  tokenUsage: SessionTokenUsageResponse | null
  /** 是否正在加载 Token 用量 */
  isLoadingTokenUsage: boolean
  /** Token 使用统计（来自监控API） */
  apiTokenUsage: TokenUsage | null
  /** 缓存命中率统计 */
  cacheStats: CacheStats | null
  /** 是否正在加载 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null
  /** 最后更新时间 */
  lastUpdated: string | null
  /** 是否启用自动刷新 */
  autoRefresh: boolean
  /** 刷新间隔（毫秒） */
  refreshInterval: number
  /** 定时器 ID */
  refreshTimer: NodeJS.Timeout | null
  /** 页面可见性事件处理器引用 */
  _visibilityHandler: (() => void) | null

  /** 获取所有监控数据 */
  fetchMonitoringData: () => Promise<void>
  /** 获取 Token 用量统计 */
  fetchTokenUsage: (sessionId: string) => Promise<void>
  /** 刷新监控数据 */
  refreshData: () => Promise<void>
  /** 设置自动刷新 */
  setAutoRefresh: (enabled: boolean) => void
  /** 设置刷新间隔 */
  setRefreshInterval: (interval: number) => void
  /** 清除错误 */
  clearError: () => void
  /** 重置状态 */
  reset: () => void
}

/**
 * 格式化当前时间
 */
function getCurrentTimestamp(): string {
  return new Date().toISOString()
}

/**
 * 监控 Store
 */
export const useMonitoringStore = create<MonitoringState>((set, get) => ({
  metrics: null,
  statistics: null,
  recentTasks: [],
  tokenUsage: null,
  isLoadingTokenUsage: false,
  apiTokenUsage: null,
  cacheStats: null,
  isLoading: false,
  error: null,
  lastUpdated: null,
  autoRefresh: true,
  refreshInterval: 5000, // 默认 5 秒刷新一次
  refreshTimer: null,
  _visibilityHandler: null,

  /**
   * 获取 Token 用量统计
   */
  fetchTokenUsage: async (sessionId: string) => {
    set({ isLoadingTokenUsage: true })

    try {
      const tokenUsage = await getSessionTotalTokenUsage(sessionId)
      set({ tokenUsage, isLoadingTokenUsage: false })
    } catch (error: unknown) {
      console.error('[MonitoringStore] 获取 Token 用量失败:', error)
      set({ isLoadingTokenUsage: false })
    }
  },

  /**
   * 获取所有监控数据
   */
  fetchMonitoringData: async () => {
    // 防止重复请求
    const state = get()
    if (state.isLoading) {
      return
    }

    set({ isLoading: true, error: null })

    try {
      // 调用 API 获取所有监控数据
      const data = await monitoringApi.getAllMonitoringData()

      set({
        metrics: data.metrics,
        statistics: data.statistics,
        recentTasks: data.recentTasks,
        apiTokenUsage: data.tokenUsage,
        cacheStats: data.cacheStats,
        isLoading: false,
        error: null,
        lastUpdated: getCurrentTimestamp(),
      })
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '获取监控数据失败'
      reportError(errorMessage, ErrorType.SERVER, undefined, {
        componentName: 'MonitoringStore',
        operation: 'fetchMonitoringData',
      })
      set({
        isLoading: false,
        error: errorMessage,
      })
    }
  },

  /**
   * 刷新监控数据
   */
  refreshData: async () => {
    await get().fetchMonitoringData()
  },

  /**
   * 设置自动刷新
   */
  setAutoRefresh: (enabled: boolean) => {
    const state = get()

    // 清除现有定时器
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer)
      set({ refreshTimer: null })
    }

    // 清除现有 visibilitychange 监听
    if (state._visibilityHandler) {
      document.removeEventListener('visibilitychange', state._visibilityHandler)
      set({ _visibilityHandler: null })
    }

    // 启用自动刷新：使用 visibilitychange 替代 setInterval
    if (enabled) {
      const handler = () => {
        if (document.visibilityState === 'visible') {
          get().fetchMonitoringData()
        }
      }
      document.addEventListener('visibilitychange', handler)
      set({
        autoRefresh: true,
        _visibilityHandler: handler,
      })
    } else {
      set({ autoRefresh: false })
    }
  },

  /**
   * 设置刷新间隔
   */
  setRefreshInterval: (interval: number) => {
    set({ refreshInterval: interval })
  },

  /**
   * 清除错误
   */
  clearError: () => {
    set({ error: null })
  },

  /**
   * 重置状态
   */
  reset: () => {
    const state = get()

    // 清除定时器
    if (state.refreshTimer) {
      clearInterval(state.refreshTimer)
    }

    set({
      metrics: null,
      statistics: null,
      recentTasks: [],
      tokenUsage: null,
      isLoadingTokenUsage: false,
      apiTokenUsage: null,
      cacheStats: null,
      isLoading: false,
      error: null,
      lastUpdated: null,
      autoRefresh: true,
      refreshInterval: 5000,
      refreshTimer: null,
    })
  },
}))
