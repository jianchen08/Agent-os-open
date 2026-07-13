/**
 * 成本控制 API 服务
 *
 * 提供成本监控、预算管理、使用统计等接口
 * 与后端 /api/v1/cost-control/* 端点对齐
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

/**
 * 预算状态响应类型
 */
export interface BudgetStatusResponse {
  /** 范围: global, user, task, session */
  scope: string
  /** 范围 ID */
  scope_id?: string
  /** Token 限制 */
  limit: number
  /** 已使用 Token */
  used: number
  /** 剩余 Token */
  remaining: number
  /** 使用率 (%) */
  usage_percent: number
  /** 告警级别 */
  alert_level: string
  /** 估算成本 ($) */
  estimated_cost: number
}

/**
 * 全局使用统计类型
 */
export interface GlobalUsageStats {
  /** 今日 Token 用量 */
  daily_tokens: number
  /** 本月 Token 用量 */
  monthly_tokens: number
  /** 每日限制 */
  daily_limit: number
  /** 每月限制 */
  monthly_limit: number
  /** 每日使用率 (%) */
  daily_usage_percent: number
  /** 每月使用率 (%) */
  monthly_usage_percent: number
  /** 今日估算成本 ($) */
  estimated_daily_cost: number
  /** 本月估算成本 ($) */
  estimated_monthly_cost: number
}

/**
 * 任务使用统计类型
 */
export interface TaskUsageStats {
  /** 任务 ID */
  task_id: string
  /** Token 用量 */
  tokens: number
  /** 限制 */
  limit: number
  /** 使用率 (%) */
  usage_percent: number
}

/**
 * 会话使用统计类型
 */
export interface SessionUsageStats {
  /** 会话 ID */
  session_id: string
  /** Token 用量 */
  tokens: number
  /** 限制 */
  limit: number
  /** 使用率 (%) */
  usage_percent: number
}

/**
 * 使用记录类型
 */
export interface UsageRecord {
  /** Token 数 */
  tokens: number
  /** 模型名称 */
  model: string
  /** 成本 ($) */
  cost: number
  /** 时间戳 */
  timestamp: string
}

/**
 * 使用统计响应类型
 */
export interface UsageStatisticsResponse {
  /** 全局统计 */
  global_stats: GlobalUsageStats
  /** 任务统计 */
  tasks: TaskUsageStats[]
  /** 会话统计 */
  sessions: SessionUsageStats[]
  /** 最近记录 */
  recent_records: UsageRecord[]
  /** 更新时间 */
  updated_at: string
}

/**
 * 成本配置响应类型
 */
export interface CostConfigResponse {
  /** 每日 Token 限制 */
  daily_token_limit: number
  /** 每月 Token 限制 */
  monthly_token_limit: number
  /** 单任务 Token 限制 */
  per_task_token_limit: number
  /** 单会话 Token 限制 */
  per_session_token_limit: number
  /** 警告阈值 */
  warning_threshold: number
  /** 严重阈值 */
  critical_threshold: number
  /** 警告时自动保存 */
  auto_save_at_warning: boolean
  /** 严重时自动暂停 */
  auto_pause_at_critical: boolean
  /** 耗尽时自动停止 */
  auto_stop_at_exhausted: boolean
}

/**
 * 成本报表响应类型
 */
export interface CostReportResponse {
  /** 统计周期: daily, weekly, monthly */
  period: string
  /** 开始日期 */
  start_date: string
  /** 结束日期 */
  end_date: string
  /** 总 Token 数 */
  total_tokens: number
  /** 总成本 ($) */
  total_cost: number
  /** 按模型统计 */
  by_model: Record<string, Record<string, any>>
  /** 按任务统计 */
  by_task: Record<string, Record<string, any>>
  /** 每日明细 */
  daily_breakdown: Record<string, any>[]
}

/**
 * 预算重置响应类型
 */
export interface BudgetResetResponse {
  /** 响应消息 */
  message: string
}

/**
 * 获取预算状态查询参数
 */
export interface BudgetStatusParams {
  /** 任务 ID */
  task_id?: string
  /** 会话 ID */
  session_id?: string
}

/**
 * 获取预算状态
 *
 * @param params 查询参数
 * @returns 预算状态
 */
export async function getBudgetStatus(params?: BudgetStatusParams): Promise<BudgetStatusResponse> {
  const response = await apiClient.get<BudgetStatusResponse>(
    API_ENDPOINTS.COST_CONTROL.BUDGET_STATUS,
    { params },
  )
  return response.data
}

/**
 * 获取使用统计
 *
 * @returns 使用统计
 */
export async function getUsageStatistics(): Promise<UsageStatisticsResponse> {
  const response = await apiClient.get<UsageStatisticsResponse>(
    API_ENDPOINTS.COST_CONTROL.USAGE_STATISTICS,
  )
  return response.data
}

/**
 * 获取成本控制配置
 *
 * @returns 成本配置
 */
export async function getCostConfig(): Promise<CostConfigResponse> {
  const response = await apiClient.get<CostConfigResponse>(API_ENDPOINTS.COST_CONTROL.CONFIG)
  return response.data
}

/**
 * 获取成本报表查询参数
 */
export interface CostReportParams {
  /** 统计周期: daily, weekly, monthly */
  period?: 'daily' | 'weekly' | 'monthly'
}

/**
 * 获取成本报表
 *
 * @param params 查询参数
 * @returns 成本报表
 */
export async function getCostReport(params?: CostReportParams): Promise<CostReportResponse> {
  const response = await apiClient.get<CostReportResponse>(API_ENDPOINTS.COST_CONTROL.REPORT, {
    params,
  })
  return response.data
}

/**
 * 重置预算
 *
 * @param params 重置参数
 * @returns 重置结果
 */
export async function resetBudget(params?: BudgetStatusParams): Promise<BudgetResetResponse> {
  const response = await apiClient.post<BudgetResetResponse>(
    API_ENDPOINTS.COST_CONTROL.BUDGET_RESET,
    null,
    { params },
  )
  return response.data
}

/**
 * 成本控制 API 对象（用于导出）
 */
export const costControlApi = {
  getBudgetStatus,
  getUsageStatistics,
  getCostConfig,
  getCostReport,
  resetBudget,
}
