/** Agent 管理 API 服务 与 agent_manager 插件 agents/* 端点对齐（生成物投影）
 *
 * 端点经 API_ENDPOINTS.AGENTS 指向 agent_manager 插件 agents*（[来源: docs/decisions/2026-08-20-agent-manager-plugin.md]）。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { UIInputFormField } from '@/types/schema'
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

/** Agent 配置字段 Schema 响应（GET agent_manager agents/schema） */
export interface AgentSchemaResponse {
  fields: UIInputFormField[]
}

/** Agent 配置写回响应（PUT agent_manager agents/{id}/config） */
export interface AgentConfigUpdateResponse {
  config_id: string
  success: boolean
  /** 备份文件名（写回前自动备份） */
  backup?: string
}

/**
 * 获取 Agent 配置字段 Schema
 *
 * 返回 agent 配置的字段级 schema（type 覆盖 string/textarea/number/select/multiselect）。
 */
export async function getAgentSchema(
  options: RetryOptions = {},
): Promise<AgentSchemaResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<AgentSchemaResponse>(API_ENDPOINTS.AGENTS.SCHEMA)
    return response.data
  }, options)
}

/**
 * 写回 Agent 配置 yaml 原文（后端先备份再写）
 *
 * @param agentId - Agent ID
 * @param yaml - 新的 yaml 内容（原文写回）
 */
export async function putAgentConfig(
  agentId: string,
  yaml: string,
  options: RetryOptions = {},
): Promise<AgentConfigUpdateResponse> {
  if (!agentId || agentId.trim().length === 0) {
    throw new Error('Agent ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.put<AgentConfigUpdateResponse>(
      API_ENDPOINTS.AGENTS.CONFIG(agentId),
      { yaml },
    )
    return response.data
  }, options)
}
