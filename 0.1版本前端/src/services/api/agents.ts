/** Agent 管理 API 服务 提供 Agent 配置的增删改查接口，与后端 /api/v1/agents/* 端点对齐 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** Agent 响应类型（与后端 AgentResponse 对齐） */
export interface AgentResponse {
  /** Agent ID */
  id: string
  /** Agent 名称 */
  name: string
  /** Agent 描述 */
  description?: string
  /** Agent 类型 */
  agent_type: string
  /** Agent 状态 */
  status: 'active' | 'inactive' | 'error'
  /** 使用的 LLM 模型 */
  model: string
  /** 系统提示词 */
  system_prompt?: string
  /** Agent 等级（如 "L1"） */
  level?: string
  /** 绑定的工具列表 */
  tool_names?: string[]
  /** 最大迭代次数 */
  max_iterations?: number
  /** 超时时间（秒） */
  timeout?: number
  /** 标签 */
  tags?: string[]
  /** 元数据 */
  metadata?: Record<string, unknown>
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at?: string

  /** 兼容字段 - agent_type 的别名 */
  type?: string
  /** 兼容字段 - tool_names 的别名 */
  tools?: string[]
  /** 兼容字段 - metadata 的别名 */
  config?: Record<string, unknown>
}

/** Agent 列表响应类型 */
export interface AgentListResponse {
  /** Agent 列表 */
  items: AgentResponse[]
  /** 总数量 */
  total: number
  /** 当前页码 */
  page: number
  /** 每页数量 */
  page_size: number
}

/** Agent 创建请求类型（与后端 AgentCreateRequest 对齐） */
export interface AgentCreateRequest {
  /** Agent 名称 */
  name: string
  /** 使用的 LLM 模型（必需） */
  model: string
  /** 系统提示词（必需） */
  system_prompt: string
  /** Agent 描述 */
  description?: string
  /** Agent 类型 */
  agent_type?: string
  /** Agent 等级（如 "L1"） */
  level?: string
  /** 绑定的工具列表 */
  tool_names?: string[]
  /** 最大迭代次数 */
  max_iterations?: number
  /** 超时时间（秒） */
  timeout?: number
  /** 标签 */
  tags?: string[]
  /** 元数据 */
  metadata?: Record<string, unknown>
}

/** Agent 更新请求类型（与后端 AgentUpdateRequest 对齐） */
export interface AgentUpdateRequest {
  /** Agent 名称 */
  name?: string
  /** Agent 描述 */
  description?: string
  /** Agent 类型 */
  agent_type?: string
  /** Agent 状态 */
  status?: 'active' | 'inactive'
  /** 使用的 LLM 模型 */
  model?: string
  /** 系统提示词 */
  system_prompt?: string
  /** Agent 等级（如 "L1"） */
  level?: string
  /** 绑定的工具列表 */
  tool_names?: string[]
  /** 最大迭代次数 */
  max_iterations?: number
  /** 超时时间（秒） */
  timeout?: number
  /** 标签 */
  tags?: string[]
  /** 元数据 */
  metadata?: Record<string, unknown>
}

/** 获取 Agent 列表查询参数 */
export interface GetAgentsParams {
  /** 页码 */
  page?: number
  /** 每页数量 */
  pageSize?: number
  /** 状态过滤 */
  status?: string
  /** 类型过滤 */
  type?: string
  /** 搜索关键词 */
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

export async function getAgent(
  agentId: string,
  options: RetryOptions = {},
): Promise<AgentResponse> {
  if (!agentId || agentId.trim().length === 0) {
    throw new Error('Agent ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<AgentResponse>(API_ENDPOINTS.AGENTS.GET(agentId))
    return response.data
  }, options)
}

export async function createAgent(
  data: AgentCreateRequest,
  options: RetryOptions = {},
): Promise<AgentResponse> {
  if (!data.name || data.name.trim().length === 0) {
    throw new Error('Agent 名称不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<AgentResponse>(API_ENDPOINTS.AGENTS.CREATE, data)
    return response.data
  }, options)
}

export async function updateAgent(
  agentId: string,
  data: AgentUpdateRequest,
  options: RetryOptions = {},
): Promise<AgentResponse> {
  if (!agentId || agentId.trim().length === 0) {
    throw new Error('Agent ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.put<AgentResponse>(API_ENDPOINTS.AGENTS.UPDATE(agentId), data)
    return response.data
  }, options)
}

export async function deleteAgent(agentId: string, options: RetryOptions = {}): Promise<void> {
  if (!agentId || agentId.trim().length === 0) {
    throw new Error('Agent ID 不能为空')
  }

  return requestWithRetry(async () => {
    await apiClient.delete(API_ENDPOINTS.AGENTS.DELETE(agentId))
  }, options)
}

export async function getDefaultAgent(options: RetryOptions = {}): Promise<AgentResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<AgentResponse>(API_ENDPOINTS.AGENTS.DEFAULT)
    return response.data
  }, options)
}
