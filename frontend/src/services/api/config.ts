/**
 * 配置管理 API 服务
 *
 * 提供 LLM 配置和上下文窗口配置的管理接口，与后端 /api/v1/config/* 端点对齐
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

export interface ModelConfig {
  provider: string
  model_name: string
  display_name: string
  api_base?: string
  /** 上下文窗口大小（token 数） */
  context_window?: number
  /** 是否推理模型（支持 thinking/reasoning） */
  reasoning_model?: boolean
  default_params?: Record<string, unknown>
}

/**
 * 提供商 API Key 条目
 *
 * 注意：后端返回时 api_key 已脱敏（mask），前端拿到的是掩码值如 `sk-****1234`。
 */
export interface ProviderKeyEntry {
  id: string
  /** API 密钥（后端返回时已脱敏） */
  api_key: string
  /** 每分钟请求数限制（0 = 不限） */
  rpm?: number
  /** Token 配额（0 = 不限） */
  token_quota?: number
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
  api_base?: string
  keys: ProviderKeyEntry[]
  /** 是否已配置可用的 API Key（按环境变量解析结果） */
  has_key?: boolean
  /** 占位符对应的环境变量名（如 OPENAI_API_KEY），明文 key 时为 null */
  env_var?: string | null
}

/** 远端模型条目（GET /llm/providers/{id}/remote-models 返回） */
export interface RemoteModel {
  /** 远端真实模型名 */
  id: string
  /** 归属方（可能为空） */
  owned_by: string
}

export interface LLMDefaults {
  /** 默认对话模型 */
  chat: string
  /** 模型分级：tier 名 → 该级默认模型 ID */
  tiers: Record<string, string>
  embedding: string
}

export interface LLMConfigResponse {
  models: Record<string, ModelConfig>
  providers: Record<string, ProviderConfig>
  defaults: LLMDefaults
}

export async function getLLMConfig(options: RetryOptions = {}): Promise<LLMConfigResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMConfigResponse>(API_ENDPOINTS.CONFIG.LLM_GET)
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

/** 更新默认模型配置（chat/embedding/tiers 可空部分更新；返回最新 defaults） */
export async function saveDefaults(
  patch: Partial<LLMDefaults>,
  options: RetryOptions = {},
): Promise<LLMDefaults> {
  return requestWithRetry(async () => {
    const response = await apiClient.put<LLMDefaults>(
      API_ENDPOINTS.CONFIG.LLM_DEFAULTS_UPDATE,
      patch,
    )
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
