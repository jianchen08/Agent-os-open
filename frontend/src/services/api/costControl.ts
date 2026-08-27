/**
 * 成本控制 API 服务
 *
 * 提供成本监控、预算管理、使用统计等接口
 * 与后端 /api/v1/cost-control/* 端点对齐
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface BudgetStatusResponse {
  /** 取值范围: global, user, task, session */
  scope: string
  scope_id?: string
  limit: number
  used: number
  remaining: number
  usage_percent: number
  alert_level: string
  /** 估算成本 ($) */
  estimated_cost: number
}

export interface GlobalUsageStats {
  daily_tokens: number
  monthly_tokens: number
  daily_limit: number
  monthly_limit: number
  daily_usage_percent: number
  monthly_usage_percent: number
  /** 今日估算成本 ($) */
  estimated_daily_cost: number
  /** 本月估算成本 ($) */
  estimated_monthly_cost: number
}

export interface TaskUsageStats {
  task_id: string
  tokens: number
  limit: number
  usage_percent: number
}

export interface SessionUsageStats {
  session_id: string
  tokens: number
  limit: number
  usage_percent: number
}

export interface UsageRecord {
  tokens: number
  model: string
  /** 成本 ($) */
  cost: number
  timestamp: string
}

export interface UsageStatisticsResponse {
  global_stats: GlobalUsageStats
  tasks: TaskUsageStats[]
  sessions: SessionUsageStats[]
  recent_records: UsageRecord[]
  updated_at: string
}

export interface CostConfigResponse {
  daily_token_limit: number
  monthly_token_limit: number
  per_task_token_limit: number
  per_session_token_limit: number
  warning_threshold: number
  critical_threshold: number
  auto_save_at_warning: boolean
  auto_pause_at_critical: boolean
  auto_stop_at_exhausted: boolean
}

export interface CostReportResponse {
  /** 统计周期: daily, weekly, monthly */
  period: string
  start_date: string
  end_date: string
  total_tokens: number
  /** 总成本 ($) */
  total_cost: number
  by_model: Record<string, Record<string, any>>
  by_task: Record<string, Record<string, any>>
  daily_breakdown: Record<string, any>[]
}

export interface BudgetResetResponse {
  message: string
}

export interface BudgetStatusParams {
  task_id?: string
  session_id?: string
}

export async function getBudgetStatus(params?: BudgetStatusParams): Promise<BudgetStatusResponse> {
  const response = await apiClient.get<BudgetStatusResponse>(
    API_ENDPOINTS.COST_CONTROL.BUDGET_STATUS,
    { params },
  )
  return response.data
}

export async function getUsageStatistics(): Promise<UsageStatisticsResponse> {
  const response = await apiClient.get<UsageStatisticsResponse>(
    API_ENDPOINTS.COST_CONTROL.USAGE_STATISTICS,
  )
  return response.data
}

export async function getCostConfig(): Promise<CostConfigResponse> {
  const response = await apiClient.get<CostConfigResponse>(API_ENDPOINTS.COST_CONTROL.CONFIG)
  return response.data
}

export interface CostReportParams {
  period?: 'daily' | 'weekly' | 'monthly'
}

export async function getCostReport(params?: CostReportParams): Promise<CostReportResponse> {
  const response = await apiClient.get<CostReportResponse>(API_ENDPOINTS.COST_CONTROL.REPORT, {
    params,
  })
  return response.data
}

export async function resetBudget(params?: BudgetStatusParams): Promise<BudgetResetResponse> {
  const response = await apiClient.post<BudgetResetResponse>(
    API_ENDPOINTS.COST_CONTROL.BUDGET_RESET,
    null,
    { params },
  )
  return response.data
}
