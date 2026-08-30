/** Agent 管理 API 服务 与 agent_manager 插件 agents/* 端点对齐（生成物投影）
 *
 * 端点经 API_ENDPOINTS.AGENTS 指向 agent_manager 插件 agents*（[来源: docs/decisions/2026-08-20-agent-manager-plugin.md]）。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** Agent 响应类型（与后端 AgentResponse 对齐） */
export interface AgentResponse {
  id: string
  name: string
  description?: string
  agent_type: string
  status: 'active' | 'inactive' | 'error'
  model: string
  system_prompt?: string
  /** Agent 等级（如 "L1"） */
  level?: string
  tool_names?: string[]
  max_iterations?: number
  /** 超时时间（秒） */
  timeout?: number
  tags?: string[]
  metadata?: Record<string, unknown>
  created_at: string
  updated_at?: string

  /** 兼容字段 - agent_type 的别名 */
  type?: string
  /** 兼容字段 - tool_names 的别名 */
  tools?: string[]
  /** 兼容字段 - metadata 的别名 */
  config?: Record<string, unknown>
}

export interface AgentListResponse {
  items: AgentResponse[]
  total: number
  page: number
  page_size: number
}

export interface GetAgentsParams {
  page?: number
  pageSize?: number
  /** 状态过滤 */
  status?: string
  /** 类型过滤 */
  type?: string
  search?: string
}

export async function getAgents(
  params: GetAgentsParams = {},
  options: RetryOptions = {},
): Promise<AgentListResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<AgentListResponse>(API_ENDPOINTS.AGENTS.LIST, {
      params: {
        page: params.page || 1,
        page_size: params.pageSize || 20,
        status: params.status,
        agent_type: params.type,
        search: params.search,
      },
    })
    return response.data
  }, options)
}
