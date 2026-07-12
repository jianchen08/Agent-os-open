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
 */
export interface ProviderConfig {
  /** 提供商类型（如 openai/deepseek/zai/minimax） */
  type: string
  /** API 基础 URL */
  api_base?: string
  /** API Key 列表（后端返回时 api_key 已脱敏） */
  keys: ProviderKeyEntry[]
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

/**
 * 压缩配置子项
 */
export interface CompressionConfig {
  /** 是否启用压缩 */
  enabled: boolean
  /** 压缩使用的模型（空则跟随主模型） */
  model: string
  /** 单层压缩触发比例 */
  layer_trigger_ratio: number
  /** 单轮次最大压缩比例 */
  max_turn_ratio: number
}

/**
 * 上下文窗口配置类型
 *
 * 与后端 config/system/context_window_config.yaml 一一对应
 */
export interface ContextWindowConfig {
  /** 配置版本 */
  version: string
  /** 压缩触发比例（占用达到此比例时触发压缩） */
  compress_trigger_ratio: number
  /** 各层 Token 预算分配（百分比，总和 = 1.0） */
  budgets: Record<string, number>
  /** 是否在 prompt 中包含工具描述 */
  include_tools_description_in_prompt: boolean
  /** 各层稳定性标记 */
  stability: Record<string, string>
  /** 层级顺序 */
  layer_order: string[]
  /** 静态变量配置 */
  static_vars: { enabled: boolean; sources: string[] }
  /** 动态变量配置 */
  dynamic_vars: {
    enabled: boolean
    vars: string[]
    rules: { enabled: boolean; hard_constraints: string[]; max_rules: number }
  }
  /** 压缩设置 */
  compression: CompressionConfig
  /** 自定义层 */
  custom_layers: Record<string, unknown>
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
  providers: Record<string, { api_base?: string; has_key: boolean }>
}> {
  return requestWithRetry(async () => {
    const response = await apiClient.get(API_ENDPOINTS.CONFIG.LLM_PROVIDERS)
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

export async function getContextWindowConfig(
  options: RetryOptions = {},
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_GET,
    )
    return response.data
  }, options)
}

export async function updateContextWindowConfig(
  data: Partial<ContextWindowConfig>,
  options: RetryOptions = {},
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_UPDATE,
      data,
    )
    return response.data
  }, options)
}

export async function resetContextWindowConfig(
  options: RetryOptions = {},
): Promise<ContextWindowConfig> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<ContextWindowConfig>(
      API_ENDPOINTS.CONFIG.CONTEXT_WINDOW_RESET,
    )
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
