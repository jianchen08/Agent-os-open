/**
 * Agent 调用记录 API 服务
 *
 * 暴露接口：
 * - listAgentCalls(params): 获取 Agent 调用记录列表
 * - getAgentCallStatistics(params): 获取 Agent 调用统计
 * - getAgentCallDetail(executionId): 获取 Agent 调用记录详情
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface AgentCallRecordResponse {
  /** 记录 ID */
  id: string
  /** 执行 ID */
  execution_id: string
  /** 调用者层级 */
  caller_level: string
  /** 目标 Agent ID */
  target_agent_id: string
  /** 目标 Agent 名称 */
  target_agent_name: string
  /** 操作类型 */
  operation_type: string
  /** 指令摘要 */
  instruction_summary: string
  /** 状态 */
  status: string
  /** 是否成功 */
  success?: boolean
  /** 结果摘要 */
  result_summary?: string
  /** 错误信息 */
  error?: string
  /** 开始时间 */
  start_time?: string
  /** 结束时间 */
  end_time?: string
  /** 执行时长（秒） */
  duration?: number
}

export interface AgentCallRecordDetailResponse extends AgentCallRecordResponse {
  /** 完整指令 */
  instruction: string
  /** 上下文 */
  context?: Record<string, any>
  /** 执行结果 */
  result?: Record<string, any>
  /** 超时时间 */
  timeout: number
  /** 重试次数 */
  retry_count: number
  /** 优先级 */
  priority: string
  /** 创建时间 */
  created_at: string
}

export interface AgentCallListResponse {
  /** 记录列表 */
  records: AgentCallRecordResponse[]
  /** 总数 */
  total: number
  /** 每页数量 */
  limit: number
  /** 偏移量 */
  offset: number
}

export interface AgentCallStatisticsResponse {
  /** 总调用次数 */
  total: number
  /** 按状态统计 */
  by_status: Record<string, number>
  /** 按调用者层级统计 */
  by_caller_level: Record<string, number>
  /** 按操作类型统计 */
  by_operation_type: Record<string, number>
  /** 成功率 (%) */
  success_rate: number
  /** 平均执行时长（秒） */
  avg_duration: number
}

export interface ListAgentCallsParams {
  /** 执行 ID */
  execution_id?: string
  /** 目标 Agent ID */
  target_agent_id?: string
  /** 调用者层级 (L1/L2) */
  caller_level?: string
  /** 状态 */
  status?: string
  /** 操作类型 */
  operation_type?: string
  /** 开始时间（范围查询） */
  start_time?: string
  /** 结束时间（范围查询） */
  end_time?: string
  /** 返回数量 */
  limit?: number
  /** 偏移量 */
  offset?: number
}

export interface GetAgentCallStatisticsParams {
  /** 目标 Agent ID */
  target_agent_id?: string
  /** 开始时间 */
  start_time?: string
  /** 结束时间 */
  end_time?: string
}

export async function listAgentCalls(
  params?: ListAgentCallsParams,
): Promise<AgentCallListResponse> {
  const response = await apiClient.get<AgentCallListResponse>(API_ENDPOINTS.AGENT_CALLS.LIST, {
    params,
  })
  return response.data
}

export async function getAgentCallStatistics(
  params?: GetAgentCallStatisticsParams,
): Promise<AgentCallStatisticsResponse> {
  const response = await apiClient.get<AgentCallStatisticsResponse>(
    API_ENDPOINTS.AGENT_CALLS.STATISTICS,
    { params },
  )
  return response.data
}

export async function getAgentCallDetail(
  executionId: string,
): Promise<AgentCallRecordDetailResponse> {
  const response = await apiClient.get<AgentCallRecordDetailResponse>(
    API_ENDPOINTS.AGENT_CALLS.GET(executionId),
  )
  return response.data
}

export const agentCallsApi = {
  listAgentCalls,
  getAgentCallStatistics,
  getAgentCallDetail,
}
