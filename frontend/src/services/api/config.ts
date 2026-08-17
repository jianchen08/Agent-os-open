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
 * - addProvider(providerId, config, options): 提供商列表 - 添加提供商
 * - deleteProvider(providerId, options): 提供商列表 - 删除提供商
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

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

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
  /** 上下文窗口大小（token 数） */
  context_window?: number
  /** 是否推理模型（支持 thinking/reasoning） */
  reasoning_model?: boolean
  /** 默认参数 */
  default_params?: Record<string, unknown>
}

/**
 * 提供商 API Key 条目
 *
 * 注意：后端返回时 api_key 已脱敏（mask），前端拿到的是掩码值如 `sk-****1234`。
 */
export interface ProviderKeyEntry {
  /** Key 标识 */
  id: string
  /** API 密钥（后端返回时已脱敏） */
  api_key: string
  /** 每分钟请求数限制（0 = 不限） */
  rpm?: number
  /** Token 配额（0 = 不限） */
  token_quota?: number
  /** 最大并发数 */
  max_concurrent?: number
}

/**
 * 提供商配置类型
 *
 * 与后端 llm.yaml 中 providers 的结构对齐。
 * 注意：api_key 字段在 keys 数组中，后端返回时已脱敏。
 * has_key/env_var 由后端按「${VAR} 占位符能否解析出真实 key」计算，
 * 未配置时 has_key=false（预置提供者的占位符不算已配置）。
 */
export interface ProviderConfig {
  /** 提供商类型（litellm 前缀，如 openai/deepseek/zai/minimax） */
  type: string
  /** API 基础 URL */
  api_base?: string
  /** API Key 列表（后端返回时 api_key 已脱敏） */
  keys: ProviderKeyEntry[]
  /** 是否已配置可用的 API Key（按环境变量解析结果） */
  has_key?: boolean
  /** 占位符对应的环境变量名（如 OPENAI_API_KEY），明文 key 时为 null */
  env_var?: string | null
}

/** 远端模型条目（GET /llm/providers/{id}/remote-models 返回） */
export interface RemoteModel {
  /** 模型 ID（远端真实模型名） */
  id: string
  /** 归属方（可能为空） */
  owned_by: string
}

/**
 * LLM 默认配置类型
 */
export interface LLMDefaults {
  /** 默认模型 */
  chat: string
  /** 模型分级 */
  tiers: Record<string, string>
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

export async function getLLMConfig(options: RetryOptions = {}): Promise<LLMConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMConfigResponse>(API_ENDPOINTS.CONFIG.LLM_GET)
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
  providers: Record<string, { api_base?: string; has_key: boolean; env_var?: string | null }>
}> {
  return requestWithRetry(async () => {
    const response = await apiClient.get(API_ENDPOINTS.CONFIG.LLM_PROVIDERS)
    return response.data
  }, options)
}

/**
 * 获取 litellm 支持的提供者类型清单
 *
 * 后端运行时读取已安装 litellm 的 provider_list——litellm pip 升级后
 * 新提供者自动出现，供「添加自定义提供商」的类型下拉使用。
 */
export async function getProviderTypes(
  options: RetryOptions = {},
): Promise<{ types: string[] }> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{ types: string[] }>(
      API_ENDPOINTS.CONFIG.LLM_PROVIDER_TYPES,
    )
    return response.data
  }, options)
}

/**
 * 从提供商 API 实时拉取该 Key 可用的模型列表
 *
 * @param providerId 提供商 ID（须已配置 Key，否则后端返回 400）
 */
export async function getRemoteModels(
  providerId: string,
  options: RetryOptions = {},
): Promise<{ provider: string; models: RemoteModel[] }> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{ provider: string; models: RemoteModel[] }>(
      API_ENDPOINTS.CONFIG.LLM_REMOTE_MODELS(providerId),
    )
    return response.data
  }, options)
}

export async function getModels(
  options: RetryOptions = {},
): Promise<{ models: Record<string, ModelConfig> }> {
  return requestWithRetry(async () => {
    const response = await apiClient.get(API_ENDPOINTS.CONFIG.LLM_MODELS)
    return response.data
  }, options)
}

export async function getDefaults(options: RetryOptions = {}): Promise<LLMDefaults> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMDefaults>(API_ENDPOINTS.CONFIG.LLM_DEFAULTS)
    return response.data
  }, options)
}

export async function saveLLMDefaults(
  defaults: LLMDefaults,
  options: RetryOptions = {},
): Promise<LLMDefaults> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<LLMDefaults>(API_ENDPOINTS.CONFIG.LLM_DEFAULTS, defaults)
    return response.data
  }, options)
}

export async function addModel(
  modelId: string,
  config: ModelConfig,
  options: RetryOptions = {},
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<Record<string, ModelConfig>>(
      API_ENDPOINTS.CONFIG.LLM_MODELS,
      { models: { [modelId]: config } },
    )
    return response.data
  }, options)
}

export async function updateModel(
  modelId: string,
  config: Partial<ModelConfig>,
  options: RetryOptions = {},
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<Record<string, ModelConfig>>(
      `${API_ENDPOINTS.CONFIG.LLM_MODELS}/${modelId}`,
      { config },
    )
    return response.data
  }, options)
}

export async function deleteModel(
  modelId: string,
  options: RetryOptions = {},
): Promise<Record<string, ModelConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.delete<Record<string, ModelConfig>>(
      `${API_ENDPOINTS.CONFIG.LLM_MODELS}/${modelId}`,
    )
    return response.data
  }, options)
}

export async function updateProviderConfig(
  providerId: string,
  config: Record<string, unknown>,
  options: RetryOptions = {},
): Promise<Record<string, ProviderConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<{ providers: Record<string, ProviderConfig> }>(
      `${API_ENDPOINTS.CONFIG.LLM_PROVIDERS}/${providerId}`,
      { config },
    )
    return response.data.providers
  }, options)
}

/**
 * 添加提供商
 *
 * 后端会将 api_key 写入 .env 文件，llm.yaml 中对应 key 改为 `${PROVIDER_ID}_API_KEY` 引用。
 *
 * @param providerId 提供商唯一标识（如 deepseek）
 * @param config 提供商配置（含 type、api_base、api_key 等）
 * @param options 重试选项
 * @returns 更新后的提供商列表
 */
export async function addProvider(
  providerId: string,
  config: { type: string; api_base?: string; api_key?: string; [key: string]: unknown },
  options: RetryOptions = {},
): Promise<Record<string, ProviderConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<{ providers: Record<string, ProviderConfig> }>(
      API_ENDPOINTS.CONFIG.LLM_PROVIDERS,
      { provider_id: providerId, config },
    )
    return response.data.providers
  }, options)
}

/**
 * 删除提供商
 *
 * @param providerId 提供商唯一标识
 * @param options 重试选项
 * @returns 更新后的提供商列表
 */
export async function deleteProvider(
  providerId: string,
  options: RetryOptions = {},
): Promise<Record<string, ProviderConfig>> {
  return requestWithRetry(async () => {
    const response = await apiClient.delete<{ providers: Record<string, ProviderConfig> }>(
      `${API_ENDPOINTS.CONFIG.LLM_PROVIDERS}/${providerId}`,
    )
    return response.data.providers
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

export async function getAPIConfig(options: RetryOptions = {}): Promise<APIConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<APIConfig>(API_ENDPOINTS.CONFIG.API_GET)
    return response.data
  }, options)
}

export async function saveAPIConfig(
  config: APIConfig,
  options: RetryOptions = {},
): Promise<APIConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<APIConfig>(API_ENDPOINTS.CONFIG.API_UPDATE, config)
    return response.data
  }, options)
}

/**
 * 探测外部 API 端点健康状态
 *
 * 直连用户配置的 base_url（第三方主机），不走 apiClient——
 * 携带本站 Authorization 头发往任意用户可填的外部地址会泄漏凭证。
 * 5s 超时，任意异常（网络/CORS/非 2xx）一律视为不可达。
 *
 * @param baseUrl 外部端点基础 URL
 * @returns 端点是否健康
 */
export async function testAPIEndpointHealth(baseUrl: string): Promise<boolean> {
  try {
    const res = await fetch(`${baseUrl.replace(/\/+$/, '')}/health`, {
      method: 'GET',
      signal: AbortSignal.timeout(5000),
    })
    return res.ok
  } catch {
    return false
  }
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
  options: RetryOptions = {},
): Promise<ConcurrencyConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ConcurrencyConfigResponse>(
      API_ENDPOINTS.CONFIG.CONCURRENCY_GET,
    )
    return response.data
  }, options)
}

export async function saveConcurrencyConfig(
  config: ConcurrencyConfigResponse,
  options: RetryOptions = {},
): Promise<ConcurrencyConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<ConcurrencyConfigResponse>(
      API_ENDPOINTS.CONFIG.CONCURRENCY_UPDATE,
      config,
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
  options: RetryOptions = {},
): Promise<CostControlConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<CostControlConfigResponse>(
      API_ENDPOINTS.CONFIG.COST_CONTROL_GET,
    )
    return response.data
  }, options)
}

export async function saveCostControlConfig(
  config: CostControlConfigResponse,
  options: RetryOptions = {},
): Promise<CostControlConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<CostControlConfigResponse>(
      API_ENDPOINTS.CONFIG.COST_CONTROL_UPDATE,
      config,
    )
    return response.data
  }, options)
}


// ---------------------------------------------------------------------------
// 通用配置（供 GenericConfigPage 使用）
// ---------------------------------------------------------------------------

/**
 * 获取通用配置
 *
 * @param configPath 配置路径（白名单中的 key，如 "system/memory_storage"）
 * @param options 重试选项
 */
export async function getGenericConfig(
  configPath: string,
  options: RetryOptions = {},
): Promise<Record<string, unknown>> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<Record<string, unknown>>(
      API_ENDPOINTS.CONFIG.GENERIC_GET(configPath),
    )
    return response.data
  }, options)
}

/**
 * 保存通用配置
 *
 * 注意：后端 GenericConfigUpdateRequest 要求 PUT body 形如 {"data": {...}}，
 * 而非裸配置对象（见 tests/e2e/test_config_rw.py::_put_config 的封装格式）。
 * 缺失 `data` 包装会被 Pydantic 拒绝并返回 422。
 *
 * @param configPath 配置路径
 * @param data 完整配置数据（裸 dict，本函数内部会包装）
 * @param options 重试选项
 */
export async function saveGenericConfig(
  configPath: string,
  data: Record<string, unknown>,
  options: RetryOptions = {},
): Promise<Record<string, unknown>> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<Record<string, unknown>>(
      API_ENDPOINTS.CONFIG.GENERIC_UPDATE(configPath),
      { data },
    )
    return response.data
  }, options)
}
