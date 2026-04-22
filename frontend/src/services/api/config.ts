/**
 * 配置管理 API 服务
 *
 * 提供 LLM 配置和上下文窗口配置的管理接口，与后端 /api/v1/config/* 端点对齐
 *
 * 暴露接口：
 * - getLLMConfig(options): LLMConfigResponse - 获取 LLM 配置
 * - getProviders(options): 提供商列表 - 获取提供商列表
 * - getModels(options): 模型列表 - 获取模型列表
 * - getDefaults(options): LLMDefaults - 获取默认配置
 * - getContextWindowConfig(options): ContextWindowConfig - 获取上下文窗口配置
 * - updateContextWindowConfig(data, options): ContextWindowConfig - 更新上下文窗口配置
 * - resetContextWindowConfig(options): ContextWindowConfig - 重置上下文窗口配置
 * - saveLLMDefaults(defaults, options): LLMDefaults - 保存 LLM 默认配置
 * - addModel(modelId, config, options): 模型列表 - 添加新模型
 * - updateModel(modelId, config, options): 模型列表 - 更新模型配置
 * - deleteModel(modelId, options): 模型列表 - 删除模型
 * - updateProviderConfig(providerId, config, options): 提供商配置 - 更新提供商配置
 * - getAPIConfig(options): APIConfig - 获取 API 配置
 * - saveAPIConfig(config, options): APIConfig - 保存 API 配置
 * - getConcurrencyConfig(options): ConcurrencyConfigResponse - 获取并发配置
 * - ModelConfig - LLM 模型配置类型
 * - ProviderConfig - 提供商配置类型
 * - LLMDefaults - LLM 默认配置类型
 * - LLMConfigResponse - LLM 配置响应类型
 * - ContextWindowConfig - 上下文窗口配置类型
 * - EndpointConfig - API 端点配置类型
 * - RateLimitConfig - 限流配置类型
 * - APIConfig - API 配置类型
 * - TaskConcurrencyConfig - 任务并发配置类型
 * - AgentConcurrencyConfig - Agent 层级并发配置类型
 * - WorkflowConcurrencyConfig - 工作流并发配置类型
 * - LLMConcurrencyConfig - LLM 并发配置类型
 * - ConcurrencyConfigResponse - 并发配置响应类型
 */

import {
  API_ENDPOINTS,
} from '@/../constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/../utils/retry'
import type { RetryOptions } from '@/../utils/retry'

/**
 * LLM 模型配置类型
 */
export interface ModelConfig {
  /** 提供商 */
  provider: string
  /** 模型名称 */
  model_name: string
  /** 显示名称 */
  display_name: string
  /** API 基础 URL */
  api_base?: string
  /** 默认参数 */
  default_params?: Record<string, any>
}

/**
 * 提供商配置类型
 */
export interface ProviderConfig {
  /** API 密钥（隐藏显示） */
  api_key: string
  /** API 基础 URL */
  api_base?: string
  /** 额外配置 */
  extra?: Record<string, any>
}

/**
 * LLM 默认配置类型
 */
export interface LLMDefaults {
  /** 聊天模型 */
  chat: string
  /** 推理模型 */
  reasoning: string
  /** 嵌入模型 */
  embedding: string
}

/**
 * LLM 配置响应类型
 */
export interface LLMConfigResponse {
  /** 模型配置 */
  models: Record<string, ModelConfig>
  /** 提供商配置 */
  providers: Record<string, ProviderConfig>
  /** 默认配置 */
  defaults: LLMDefaults
}

/**
 * 上下文窗口配置类型
 */
export interface ContextWindowConfig {
  /** 最大上下文长度 */
  max_context_length: number
  /** 保留的系统消息数 */
  reserved_system_messages: number
  /** 保留的最近消息数 */
  reserved_recent_messages: number
  /** 摘要阈值 */
  summary_threshold: number
}

export async function getLLMConfig(
  options: RetryOptions = {}
): Promise<LLMConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMConfigResponse>(
      API_ENDPOINTS.CONFIG.LLM_GET
    )
    return response.data
  }, options)
}

/**
 * 获取提供商列表
 *
 * @param options 重试选项
 * @returns 提供商列表
 */
export async function getProviders(options: RetryOptions = {}): Promise<{
  providers: Record<string, { api_base?: string; has_key: boolean }>
}> {
  return requestWithRetry(async () => {
    const response = await apiClient.get(API_ENDPOINTS.CONFIG.LLM_PROVIDERS)
    return response.data
  }, options)
}

export async function getModels(
  options: RetryOptions = {}
): Promise<{ models: Record<string, ModelConfig> }> {
  return requestWithRetry(async () => {
    const response = await apiClient.get(API_ENDPOINTS.CONFIG.LLM_MODELS)
    return response.data
  }, options)
}

export async function getDefaults(
  options: RetryOptions = {}
): Promise<LLMDefaults> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMDefaults>(
      API_ENDPOINTS.CONFIG.LLM_DEFAULTS
    )
    return response.data
  }, options)
}

export async function getContextWindowConfig(
  options: RetryOptions = {}
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_GET
    )
    return response.data
  }, options)
}

export async function updateContextWindowConfig(
  data: Partial<ContextWindowConfig>,
  options: RetryOptions = {}
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_UPDATE,
      data
    )
    return response.data
  }, options)
}

export async function resetContextWindowConfig(
  options: RetryOptions = {}
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_RESET
    )
    return response.data
  }, options)
}

export async function saveLLMDefaults(
  defaults: LLMDefaults,
  options: RetryOptions = {}
): Promise<LLMDefaults> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<LLMDefaults>(
      API_ENDPOINTS.CONFIG.LLM_DEFAULTS,
      defaults
    )
    return response.data
  }, options)
}

export async function addModel(
  modelId: string,
  config: ModelConfig,
  options: RetryOptions = {}
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<Record<string, ModelConfig>>(
      API_ENDPOINTS.CONFIG.LLM_MODELS,
      { [modelId]: config }
    )
    return response.data
  }, options)
}

export async function updateModel(
  modelId: string,
  config: Partial<ModelConfig>,
  options: RetryOptions = {}
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<Record<string, ModelConfig>>(
      `${API_ENDPOINTS.CONFIG.LLM_MODELS}/${modelId}`,
      config
    )
    return response.data
  }, options)
}

export async function deleteModel(
  modelId: string,
  options: RetryOptions = {}
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.delete<Record<string, ModelConfig>>(
      `${API_ENDPOINTS.CONFIG.LLM_MODELS}/${modelId}`
    )
    return response.data
  }, options)
}

export async function updateProviderConfig(
  providerId: string,
  config: Partial<ProviderConfig>,
  options: RetryOptions = {}
): Promise<Record<string, ProviderConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<Record<string, ProviderConfig>>(
      `${API_ENDPOINTS.CONFIG.LLM_PROVIDERS}/${providerId}`,
      config
    )
    return response.data
  }, options)
}

/**
 * API 端点配置类型
 */
export interface EndpointConfig {
  /** 基础 URL */
  base_url: string
  /** API 版本 */
  version: string
  /** 超时时间（秒） */
  timeout: number
}

/**
 * 限流配置类型
 */
export interface RateLimitConfig {
  /** 全局限流 */
  global_limit: string
  /** 认证限流 */
  auth: string
  /** 任务限流 */
  tasks: string
  /** WebSocket 限流 */
  websocket: string
}

/**
 * API 配置类型
 */
export interface APIConfig {
  /** 端点配置 */
  endpoint: EndpointConfig
  /** 限流配置 */
  rate_limit: RateLimitConfig
  /** CORS 允许的源 */
  cors_origins: string[]
}

export async function getAPIConfig(
  options: RetryOptions = {}
): Promise<APIConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<APIConfig>(
      API_ENDPOINTS.CONFIG.API_GET
    )
    return response.data
  }, options)
}

export async function saveAPIConfig(
  config: APIConfig,
  options: RetryOptions = {}
): Promise<APIConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<APIConfig>(
      API_ENDPOINTS.CONFIG.API_UPDATE,
      config
    )
    return response.data
  }, options)
}

/**
 * 任务并发配置类型
 */
export interface TaskConcurrencyConfig {
  /** 最大并发任务数 */
  max_concurrent_tasks: number
  /** 任务执行线程池大小 */
  task_max_workers: number
  /** 任务超时（秒） */
  task_timeout: number
}

/**
 * Agent 层级并发配置类型
 */
export interface AgentConcurrencyConfig {
  /** L1 Agent (项目经理) 最大并发数 */
  l1_max_concurrent: number
  /** L2 Agent (团队负责人) 最大并发数 */
  l2_max_concurrent: number
  /** L3 Agent (执行者) 最大并发数 */
  l3_max_concurrent: number
}

/**
 * 工作流并发配置类型
 */
export interface WorkflowConcurrencyConfig {
  /** 工作流最大并发数 */
  max_concurrent: number
}

/**
 * LLM 并发配置类型
 */
export interface LLMConcurrencyConfig {
  /** 智谱 AI 最大并发数 */
  zhipu_max_concurrent: number
  /** OpenAI 最大并发数 */
  openai_max_concurrent: number
  /** Anthropic 最大并发数 */
  anthropic_max_concurrent: number
  /** 默认最大并发数 */
  default_max_concurrent: number
}

/**
 * 并发配置响应类型
 */
export interface ConcurrencyConfigResponse {
  /** 任务并发配置 */
  task: TaskConcurrencyConfig
  /** Agent 层级并发配置 */
  agent: AgentConcurrencyConfig
  /** 工作流并发配置 */
  workflow: WorkflowConcurrencyConfig
  /** LLM 并发配置 */
  llm: LLMConcurrencyConfig
}

export async function getConcurrencyConfig(
  options: RetryOptions = {}
): Promise<ConcurrencyConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ConcurrencyConfigResponse>(
      API_ENDPOINTS.CONFIG.CONCURRENCY_GET
    )
    return response.data
  }, options)
}

export interface CostControlGlobalConfig {
  daily_token_limit: number
  monthly_token_limit: number
  per_task_token_limit: number
  per_session_token_limit: number
}

export interface CostControlAlertsConfig {
  warning_threshold: number
  critical_threshold: number
  exhausted_threshold: number
}

export interface CostControlProtectionConfig {
  auto_save_at_warning: boolean
  auto_pause_at_critical: boolean
  auto_stop_at_exhausted: boolean
}

export interface CostControlConfigResponse {
  global_config: CostControlGlobalConfig
  alerts: CostControlAlertsConfig
  protection: CostControlProtectionConfig
  enabled: boolean
}

export async function getCostControlConfig(
  options: RetryOptions = {}
): Promise<CostControlConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<CostControlConfigResponse>(
      API_ENDPOINTS.CONFIG.COST_CONTROL_GET
    )
    return response.data
  }, options)
}

export async function saveCostControlConfig(
  config: CostControlConfigResponse,
  options: RetryOptions = {}
): Promise<CostControlConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<CostControlConfigResponse>(
      API_ENDPOINTS.CONFIG.COST_CONTROL_UPDATE,
      config
    )
    return response.data
  }, options)
}
