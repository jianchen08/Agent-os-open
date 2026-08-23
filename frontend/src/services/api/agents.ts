/** Agent 管理 API 服务 与 agent_manager 插件 agents/* 端点对齐（生成物投影）
 *
 * 2026-08-19 清理：getAgent/createAgent/updateAgent/deleteAgent/getDefaultAgent 指向
 * 后端不存在的端点，已连同其用例删除。
 * 2026-08-20 插件化：原内核 /api/v1/agents* 4 路由迁至 agent_manager 插件
 * （agent_manager 插件 agents*，ADR 2026-08-20），本服务经 API_ENDPOINTS.AGENTS 随切。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { UIInputFormField } from '@/types/schema'
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

/** Agent 配置字段 Schema 响应（GET agent_manager agents/schema） */
export interface AgentSchemaResponse {
  /** 字段定义（表单驱动） */
  fields: UIInputFormField[]
}

/** Agent 配置读取响应（GET agent_manager agents/{id}/config） */
export interface AgentConfigResponse {
  /** Agent ID */
  config_id: string
  /** 配置文件 yaml 原文 */
  yaml: string
}

/** Agent 配置写回响应（PUT agent_manager agents/{id}/config） */
export interface AgentConfigUpdateResponse {
  /** Agent ID */
  config_id: string
  /** 是否写回成功 */
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
 * 读取 Agent 配置 yaml 原文
 *
 * @param agentId - Agent ID（对应 config/agents 目录下的 yaml 配置文件名）
 */
export async function getAgentConfig(
  agentId: string,
  options: RetryOptions = {},
): Promise<AgentConfigResponse> {
  if (!agentId || agentId.trim().length === 0) {
    throw new Error('Agent ID 不能为空')
  }

  return requestWithRetry(async () => {
    const response = await apiClient.get<AgentConfigResponse>(API_ENDPOINTS.AGENTS.CONFIG(agentId))
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
