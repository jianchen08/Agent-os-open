/**
 * 配置类型定义
 */

/** LLM 模型配置 */
export interface LLMModel {
  name: string
  provider: string
  api_base?: string
  api_key?: string
  max_tokens?: number
  temperature?: number
}

/** LLM 提供商 */
export interface LLMProvider {
  name: string
  models: string[]
  api_base: string
}

/** LLM 配置响应 */
export interface LLMConfigResponse {
  models: LLMModel[]
  providers: LLMProvider[]
  default_model: string
}

/** API Key 配置 */
export interface APIKeyConfig {
  provider: string
  api_key: string
}

/** 模型默认参数 */
export interface ModelDefaultParams {
  temperature: number
  max_tokens: number
  top_p?: number
  frequency_penalty?: number
  presence_penalty?: number
}

/** LLM 模型表单数据 */
export interface LLMModelFormData {
  name: string
  provider: string
  api_base: string
  max_tokens: number
  temperature: number
}
