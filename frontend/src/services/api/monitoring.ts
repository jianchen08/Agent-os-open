/**
 * 监控 API 服务
 *
 * 提供系统监控、任务统计等 API 接口
 *
 * 暴露接口：
 * - getSystemMetrics(options): SystemMetrics - 获取系统性能指标
 * - getTaskStatistics(options): TaskStatistics - 获取任务执行统计
 * - getTaskList(page, pageSize, status, options): 任务列表 - 获取任务列表
 * - getAllMonitoringData(options): 所有监控数据 - 获取所有监控数据（汇总接口）
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type {
  CacheStats,
  CacheStatsResponse,
  SystemMetrics,
  SystemMetricsResponse,
  TaskInfo,
  TaskListResponse,
  TaskStatistics,
  TaskStatisticsResponse,
  TokenUsage,
  TokenUsageResponse,
} from '@/types/monitoring'
import type { RetryOptions } from '@/utils/retry'

export async function getSystemMetrics(options: RetryOptions = {}): Promise<SystemMetrics> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<SystemMetricsResponse>(
      API_ENDPOINTS.MONITORING.SYSTEM_METRICS,
    )
    return response.data.metrics
  }, options)
}

export async function getTaskStatistics(options: RetryOptions = {}): Promise<TaskStatistics> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<TaskStatisticsResponse>(
      API_ENDPOINTS.MONITORING.TASK_STATISTICS,
    )
    return response.data.statistics
  }, options)
}

export async function getTaskList(
  page: number = 1,
  pageSize: number = 20,
  status?: string,
  options: RetryOptions = {},
): Promise<{ items: TaskInfo[]; total: number }> {
  return requestWithRetry(async () => {
    const params: Record<string, string | number> = {
      page,
      page_size: pageSize,
    }
    if (status) {
      params.status = status
    }

    const response = await apiClient.get<TaskListResponse>(API_ENDPOINTS.MONITORING.TASK_LIST, {
      params,
    })
    return {
      items: response.data.items,
      total: response.data.total,
    }
  }, options)
}

export async function getTokenUsage(options: RetryOptions = {}): Promise<TokenUsage> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<TokenUsageResponse>(
      API_ENDPOINTS.MONITORING.TOKEN_USAGE,
    )
    return response.data.token_usage
  }, options)
}

export async function getCacheStats(options: RetryOptions = {}): Promise<CacheStats> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<CacheStatsResponse>(
      API_ENDPOINTS.MONITORING.CACHE_STATS,
    )
    return response.data.cache_stats
  }, options)
}

export async function getAllMonitoringData(options: RetryOptions = {}): Promise<{
  metrics: SystemMetrics | null
  statistics: TaskStatistics | null
  recentTasks: TaskInfo[]
  tokenUsage: TokenUsage | null
  cacheStats: CacheStats | null
}> {
  try {
    // 并行请求所有数据
    const [metrics, statistics, tasksResult, tokenUsageResult, cacheStatsResult] = await Promise.allSettled([
      getSystemMetrics(options),
      getTaskStatistics(options),
      getTaskList(1, 10, undefined, options),
      getTokenUsage(options),
      getCacheStats(options),
    ])

    return {
      metrics: metrics.status === 'fulfilled' ? metrics.value : null,
      statistics: statistics.status === 'fulfilled' ? statistics.value : null,
      recentTasks: tasksResult.status === 'fulfilled' ? tasksResult.value.items : [],
      tokenUsage: tokenUsageResult.status === 'fulfilled' ? tokenUsageResult.value : null,
      cacheStats: cacheStatsResult.status === 'fulfilled' ? cacheStatsResult.value : null,
    }
  } catch (error) {
    console.error('[MonitoringAPI] 获取监控数据失败:', error)
    throw error
  }
}
