/**
 * 配置管理 API 服务
 *
 * LLM 配置（llm.yaml）读写走**内核单一配置面** GET/PUT
 * /api/v1/plugins/llm_service/config/llm（掩码 + ETag 乐观锁 + 写恒用户空间
 * + 首写播种 + 接管账本，ADR 2026-09-13/14）；provider 明文 key 经通用
 * /api/v1/config/env 落用户空间 .env（yaml 保持 ${VAR} 占位引用）。
 * 插件侧 /ext 文件 IO 端点已退役（2026-09-28 配置读写单源化批次 A1，
 * 方案：docs/working/LLM配置改不生效修复方案_20260928.md）。
 *
 * 只读声明面（presets/provider-types/remote-models）仍由 llm_service 插件
 * /ext 端点承载（无文件 IO）。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { getPluginConfigFile, savePluginConfigFile } from '@/services/api/pluginConfig'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** 内核单一配置面上 llm_service 的 llm.yaml（manifest config_files[].id） */
const LLM_PLUGIN_ID = 'llm_service'
const LLM_FILE_ID = 'llm'

/** GET 掩码标记（内核 mask_secrets 对 secret 形字段打码） */
const MASK_MARK = '****'
/** .env.example 示例值前缀——视为未配置，绝不写回 */
const EXAMPLE_PREFIX = 'your-'
/** ${VAR} 整串占位符（provider key 在 yaml 里的存储形态） */
const ENV_REF_RE = /^\$\{(\w+)\}$/

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
  /** 思考强度→参数映射（high/medium/low → thinking/reasoning_effort；空档位回退 llm_core 内置默认表） */
  thinking_strength_params?: Record<string, Record<string, unknown>>
  /** 多模态能力声明（supports_image/supported_image_types/max_image_size 等，multimodal 插件消费） */
  multimodal?: Record<string, unknown>
}

/**
 * 提供商 API Key 条目
 *
 * 注意：内核 GET 返回时 api_key 已按 secret 形字段掩码（***），
 * ${VAR} 占位符原样透传。
 */
export interface ProviderKeyEntry {
  id: string
  /** API 密钥（内核返回时已掩码） */
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
 * 与 llm.yaml 中 providers 的结构对齐。
 * has_key/env_var 由本服务层按「${VAR} 占位符能否在用户 .env 掩码视图中
 * 解析」计算，未配置时 has_key=false（预置提供者的占位符不算已配置）。
 */
export interface ProviderConfig {
  /** 提供商类型（litellm 前缀，如 openai/deepseek/zai/minimax） */
  type: string
  api_base?: string
  /** 旧版顶层明文 key（迁移前 yaml 形态；extractApiKeyToEnv 迁移后由 keys[0] 承载） */
  api_key?: string
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
  /** 上下文窗口（litellm 注册表事实；查不到则缺席，不发明数字） */
  context_window?: number
  /** 最大输出 tokens（同上；查不到则缺席） */
  max_output_tokens?: number
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

// ── 内核单一配置面读写原语 ────────────────────────────────

/** 用户空间 .env 掩码视图：变量名 → 是否已设置。has_key 派生与 key 写入共用。 */
async function getUserEnvVarSet(): Promise<Set<string>> {
  const response = await apiClient.get<{ vars: Record<string, string> }>(
    API_ENDPOINTS.PLUGIN_CONFIG.USER_ENV,
  )
  return new Set(Object.keys(response.data.vars ?? {}))
}

/** 写用户空间 .env（set 写值 / unset 清除；*** 哨兵由后端保留现值）。 */
async function putUserEnv(
  update: { set?: Record<string, string>; unset?: string[] },
): Promise<void> {
  await apiClient.put(API_ENDPOINTS.PLUGIN_CONFIG.USER_ENV, update)
}

/**
 * 读-改-写 llm.yaml 单一入口：GET（掩码视图 + ETag）→ edit 原地改树 →
 * PUT（If-Match 乐观锁，409 冲突由 PluginConfigConflictError 透传）。
 */
async function editLlmFile<T>(
  edit: (data: Record<string, unknown>) => T,
  options: RetryOptions = {},
): Promise<T> {
  return requestWithRetry(async () => {
    const { data: file, etag } = await getPluginConfigFile(LLM_PLUGIN_ID, LLM_FILE_ID)
    const result = await edit(file.data)
    await savePluginConfigFile(LLM_PLUGIN_ID, LLM_FILE_ID, file.data, etag)
    return result
  }, options)
}

/** provider 的 (has_key, env_var) 派生——与原插件 _provider_key_status 同判定：
 *  keys[0]（次顶层）api_key 为 ${VAR} 占位 → 按 .env 掩码视图判定；掩码值
 *  （磁盘明文 key 的视图）视为已配置；空 → 未配置。 */
function deriveKeyStatus(
  pconf: ProviderConfig,
  envSet: Set<string>,
): { has_key: boolean; env_var: string | null } {
  const first = pconf.keys?.[0]
  const raw = first?.api_key || pconf.api_key || ''
  const varName = typeof raw === 'string' ? ENV_REF_RE.exec(raw.trim())?.[1] : undefined
  if (varName) {
    return { has_key: envSet.has(varName), env_var: varName }
  }
  // 磁盘明文 key 在内核掩码视图中呈 '****'——视为已配置；空 → 未配置
  return { has_key: typeof raw === 'string' && raw.length > 0, env_var: null }
}

/** yaml 内明文 key 提取（与原插件 _extract_api_key_to_env 同语义）：
 *  明文 → 写用户 .env + 改 ${VAR} 占位；掩码/示例值 → 剔除防污染。 */
async function extractApiKeyToEnv(
  providerId: string,
  config: Record<string, unknown>,
): Promise<void> {
  const envVarName = `${providerId.toUpperCase()}_API_KEY`
  const placeholder = '${' + envVarName + '}'
  const keys = config.keys
  const topLevelKey = config.api_key
  if (typeof topLevelKey === 'string' && topLevelKey && !isPlaceholderValue(topLevelKey)) {
    await putUserEnv({ set: { [envVarName]: topLevelKey } })
    config.keys = [{ id: `${providerId}_main`, api_key: placeholder }]
    delete config.api_key
    return
  }
  if (Array.isArray(keys) && keys.length > 0 && typeof keys[0] === 'object' && keys[0] !== null) {
    const k0 = keys[0] as Record<string, unknown>
    const raw = k0.api_key
    if (typeof raw === 'string' && raw && !ENV_REF_RE.test(raw.trim())) {
      if (isPlaceholderValue(raw)) {
        delete k0.api_key
      } else {
        await putUserEnv({ set: { [envVarName]: raw } })
        k0.api_key = placeholder
      }
    }
  }
}

function isPlaceholderValue(value: unknown): boolean {
  return typeof value === 'string' && (value.includes(MASK_MARK) || value.startsWith(EXAMPLE_PREFIX))
}

/** 读全量配置（内核文件视图）+ provider key 状态派生。 */
export async function getLLMConfig(options: RetryOptions = {}): Promise<LLMConfigResponse> {
  return requestWithRetry(async () => {
    const { data: file } = await getPluginConfigFile(LLM_PLUGIN_ID, LLM_FILE_ID)
    const providers = ((file.data ?? {}).providers ?? {}) as Record<string, ProviderConfig>
    const envSet = await getUserEnvVarSet()
    for (const pconf of Object.values(providers)) {
      const { has_key, env_var } = deriveKeyStatus(pconf, envSet)
      pconf.has_key = has_key
      pconf.env_var = env_var
    }
    return {
      models: ((file.data ?? {}).models ?? {}) as Record<string, ModelConfig>,
      providers,
      defaults: ((file.data ?? {}).defaults ?? { chat: '', embedding: '', tiers: {} }) as LLMDefaults,
    }
  }, options)
}

/** LLM 配置面预置声明（llm_service 插件下发，前端设置页唯一来源；P2-5） */
export interface LLMPresetGroup {
  /** 分组标题（如「国内」「国际」） */
  label: string
  /** [provider_id, 显示名] 二元组 */
  providers: [string, string][]
}

export interface LLMPresets {
  provider_groups: LLMPresetGroup[]
  /** 「添加自定义提供商」类型下拉的常用置顶 */
  common_provider_types: string[]
  /** 思考强度映射白名单（levels=档位词汇；allowed_keys=允许覆盖的参数键） */
  thinking_strength: {
    levels: string[]
    allowed_keys: string[]
  }
}

/**
 * 拉取 LLM 配置面预置声明
 *
 * 新增预置厂商仅改 llm_service 声明文件（llm_presets.yaml）+ llm.yaml，
 * 前端零改动。
 */
export async function getLLMPresets(options: RetryOptions = {}): Promise<LLMPresets> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<LLMPresets>(API_ENDPOINTS.CONFIG.LLM_PRESETS)
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
  const config = await getLLMConfig(options)
  return { models: config.models }
}

export async function getDefaults(options: RetryOptions = {}): Promise<LLMDefaults> {
  const config = await getLLMConfig(options)
  return config.defaults
}

/** 更新默认模型配置（chat/embedding/tiers 可空部分更新；返回最新 defaults） */
export async function saveDefaults(
  patch: Partial<LLMDefaults>,
  options: RetryOptions = {},
): Promise<LLMDefaults> {
  return editLlmFile((data) => {
    const current = (data.defaults ?? { chat: '', embedding: '', tiers: {} }) as LLMDefaults
    const defaults = { ...current, ...patch }
    data.defaults = defaults
    return defaults
  }, options)
}

/**
 * 添加模型（模型 ID 已存在时按原后端规则派生：同 provider 同 model_name
 * 真重复报错；否则 `<id>-<provider>` 起步追加序号，绝不覆盖既有条目）。
 */
export async function addModel(
  modelId: string,
  config: ModelConfig,
  options: RetryOptions = {},
): Promise<{ models: Record<string, ModelConfig>; added_ids: string[] }> {
  return editLlmFile((data) => {
    const models = (data.models ?? {}) as Record<string, ModelConfig>
    let targetId = modelId
    if (models[modelId]) {
      const existing = models[modelId]
      if (existing.provider === config.provider && existing.model_name === config.model_name) {
        throw new Error(`模型 '${modelId}' 已存在于提供商 '${existing.provider}'`)
      }
      const base = `${modelId}-${config.provider || 'custom'}`
      targetId = base
      let seq = 2
      while (models[targetId]) {
        targetId = `${base}-${seq}`
        seq += 1
      }
    }
    models[targetId] = config
    data.models = models
    return { models, added_ids: [targetId] }
  }, options)
}

export async function updateModel(
  modelId: string,
  config: Partial<ModelConfig>,
  options: RetryOptions = {},
): Promise<Record<string, ModelConfig>> {
  return editLlmFile((data) => {
    const models = (data.models ?? {}) as Record<string, ModelConfig>
    if (!models[modelId]) {
      throw new Error(`模型 '${modelId}' 不存在`)
    }
    models[modelId] = { ...models[modelId], ...config }
    data.models = models
    return models
  }, options)
}

export async function deleteModel(
  modelId: string,
  options: RetryOptions = {},
): Promise<Record<string, ModelConfig>> {
  return editLlmFile((data) => {
    const models = (data.models ?? {}) as Record<string, ModelConfig>
    if (!models[modelId]) {
      throw new Error(`模型 '${modelId}' 不存在`)
    }
    delete models[modelId]
    data.models = models
    return models
  }, options)
}

/**
 * 更新提供商配置。
 *
 * 提交中的明文 api_key（顶层或 keys[0].api_key）写用户空间 .env、yaml 保持
 * ${VAR} 占位；掩码/示例值剔除防污染。keys 数组按索引合并（未提交字段保留
 * 磁盘值）——只改并发/RPM 时不带 api_key，占位符不会被清掉。
 */
export async function updateProviderConfig(
  providerId: string,
  config: Record<string, unknown>,
  options: RetryOptions = {},
): Promise<Record<string, ProviderConfig>> {
  return editLlmFile(async (data) => {
    const providers = (data.providers ?? {}) as Record<string, ProviderConfig>
    const existing = providers[providerId]
    if (!existing) {
      throw new Error(`提供商 '${providerId}' 不存在`)
    }
    const patch = { ...config } as Record<string, unknown>
    await extractApiKeyToEnv(providerId, patch)
    const newKeys = patch.keys
    if (Array.isArray(newKeys) && Array.isArray(existing.keys) && existing.keys.length > 0) {
      const merged = newKeys.map((entry: unknown, i: number) => {
        if (entry !== null && typeof entry === 'object') {
          // yaml 节点互转：keys 条目是异构 yaml 树，经 unknown 中转按字段合并
          const base =
            i < existing.keys.length && existing.keys[i] && typeof existing.keys[i] === 'object'
            ? { ...(existing.keys[i] as unknown as Record<string, unknown>) }
            : {}
          for (const [k, v] of Object.entries(entry as Record<string, unknown>)) {
            if (v !== null) base[k] = v
          }
          return base
        }
        return entry
      })
      patch.keys = merged
    }
    providers[providerId] = { ...existing, ...patch } as ProviderConfig
    data.providers = providers
    return providers
  }, options)
}

/**
 * 添加提供商
 *
 * 明文 api_key 写用户空间 .env，llm.yaml 中对应 key 为 `${PROVIDER_ID}_API_KEY` 引用。
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
  return editLlmFile(async (data) => {
    const providers = (data.providers ?? {}) as Record<string, ProviderConfig>
    if (providers[providerId]) {
      throw new Error(`提供商 '${providerId}' 已存在`)
    }
    const patch = { ...config } as Record<string, unknown>
    await extractApiKeyToEnv(providerId, patch)
    providers[providerId] = patch as unknown as ProviderConfig
    data.providers = providers
    return providers
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
  return editLlmFile((data) => {
    const providers = (data.providers ?? {}) as Record<string, ProviderConfig>
    if (!providers[providerId]) {
      throw new Error(`提供商 '${providerId}' 不存在`)
    }
    delete providers[providerId]
    data.providers = providers
    return providers
  }, options)
}
