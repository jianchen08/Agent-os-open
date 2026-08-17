/**
 * 插件配置 API 服务
 *
 * 对接后端 P1-4/P1-5 端点（ADR §4.3）：
 * - GET  /api/v1/plugins/{id}/config/{file_id}：读配置文件，返回掩码后内容 + ETag（B4 乐观锁）
 * - PUT  /api/v1/plugins/{id}/config/{file_id}：写配置文件，ETag 不匹配返回 409
 *
 * 配置树（plugin_configs）聚合在 /api/v1/schema 响应中，由本服务的 getPluginConfigs 提取。
 *
 * 注意：本服务只做"连接 + 数据"层——不渲染表单、不做布局。
 *
 * @module api/pluginConfig
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import type { SchemaResponse } from '@/services/api/schema'

/**
 * config_files.fields 单字段声明（manifest 前端镜像）。
 *
 * env target：name = .env 键，type 仅 secret|string。
 * YAML target：name 支持点号路径（如 defaults.chat），type 为 UI 表单词汇
 * （select/toggle/number/textarea…），options/min/max/step/default 等 UI 词汇
 * 由内核 flatten 透传（kernel EnvConfigField::extra）。
 */
export interface EnvConfigFieldDef {
  name: string
  label: string
  type?: string
  required?: boolean
  description?: string
  /** 选项（select/multiselect/radio/checkbox） */
  options?: Array<{ label?: string; value: string | number }>
  min?: number
  max?: number
  step?: number
  default?: unknown
  placeholder?: string
  /** 动态数据源 URI（选项从端点拉取） */
  datasourceUri?: string
  validation?: { min?: number; max?: number; pattern?: string; message?: string }
  [key: string]: unknown
}

export interface PluginConfigFileMapping {
  id: string
  path: string
  label: string
  /** GAP-4：写入目标（"env" = key/加密字段写 .env，前端按 fields 渲染密钥表单） */
  target?: 'env'
  fields?: EnvConfigFieldDef[]
}

/** schema.plugin_configs 数组元素 */
export interface PluginConfigEntry {
  plugin_id: string
  plugin_name: string
  config_files: PluginConfigFileMapping[]
}

/** GET 配置文件响应体（与后端 PluginConfigResponse 对齐） */
export interface PluginConfigFileResponse {
  plugin_id: string
  file_id: string
  label: string
  path: string
  data: Record<string, unknown>
  etag: string
}

/** getPluginConfigFile 的返回（data + 从响应头/体提取的 etag） */
export interface PluginConfigFileResult {
  data: PluginConfigFileResponse
  etag: string
}

/** savePluginConfigFile 的返回（新 etag） */
export interface PluginConfigSaveResult {
  etag: string
}

/**
 * 409 ETag 冲突错误。
 *
 * 当 PUT 的 if_match 与磁盘当前 ETag 不一致时后端返回 409，本错误携带
 * 服务端返回的当前 etag（currentEtag），供 UI 提示"已被他人修改，是否覆盖"。
 */
export class PluginConfigConflictError extends Error {
  /** 后端返回的当前 ETag（响应头 etag，可能缺失） */
  readonly currentEtag: string | undefined

  constructor(message: string, currentEtag?: string) {
    super(message)
    this.name = 'PluginConfigConflictError'
    this.currentEtag = currentEtag
  }

  /** 判定一个错误是否为配置冲突错误。 */
  static is(error: unknown): error is PluginConfigConflictError {
    return (
      error instanceof Error &&
      (error as PluginConfigConflictError).name === 'PluginConfigConflictError'
    )
  }
}

/** 兼容：也导出一个 is 谓词函数（避免命名风格分歧）。 */
export function isPluginConfigConflict(error: unknown): error is PluginConfigConflictError {
  return PluginConfigConflictError.is(error)
}

/** 从 axios 响应对象取 ETag（优先响应头，回退响应体 etag）。 */
function extractEtag(
  headers: Record<string, string> | undefined,
  bodyEtag: string | undefined,
): string {
  const headerEtag = headers?.etag
  if (headerEtag && headerEtag.length > 0) return headerEtag
  return bodyEtag ?? ''
}

/**
 * 从 schema 聚合响应提取插件配置树。
 *
 * 仅含声明了 config_files 的插件。schema 无 plugin_configs 字段时返回空数组。
 *
 * @param schema - /api/v1/schema 聚合响应
 * @returns 插件配置条目数组
 */
export function getPluginConfigs(schema: SchemaResponse): PluginConfigEntry[] {
  const entries = (schema as SchemaResponse & { plugin_configs?: PluginConfigEntry[] }).plugin_configs
  if (!Array.isArray(entries)) return []
  return entries
}

/**
 * GET 单个配置文件内容 + ETag。
 *
 * @param pluginId - 插件 id
 * @param fileId - 配置文件 id（manifest config_files[].id）
 * @returns 配置文件内容与 ETag
 */
export async function getPluginConfigFile(
  pluginId: string,
  fileId: string,
): Promise<PluginConfigFileResult> {
  const response = await apiClient.get<PluginConfigFileResponse>(
    API_ENDPOINTS.PLUGIN_CONFIG.FILE(pluginId, fileId),
  )
  const etag = extractEtag(response.headers, response.data.etag)
  return { data: response.data, etag }
}

/**
 * PUT 保存配置文件（乐观锁）。
 *
 * @param pluginId - 插件 id
 * @param fileId - 配置文件 id
 * @param data - 完整文件内容（已掩码 *** 哨兵由后端 B2 合并）
 * @param ifMatch - GET 时拿到的 ETag；不匹配时后端返回 409
 * @returns 保存成功后的新 ETag
 * @throws {PluginConfigConflictError} ETag 冲突（409）
 * @throws 原样透传其他 axios 错误（404/500 等）
 */
export async function savePluginConfigFile(
  pluginId: string,
  fileId: string,
  data: Record<string, unknown>,
  ifMatch?: string,
): Promise<PluginConfigSaveResult> {
  try {
    const response = await apiClient.put<{ etag?: string } & Record<string, unknown>>(
      API_ENDPOINTS.PLUGIN_CONFIG.FILE(pluginId, fileId),
      { data, if_match: ifMatch },
    )
    const etag = extractEtag(response.headers, response.data.etag)
    return { etag }
  } catch (error) {
    // 409：ETag 冲突，包装为业务错误（携带当前 etag 供 UI 提示）
    const status = (error as { response?: { status?: number; headers?: Record<string, string> } }).response?.status
    if (status === 409) {
      const headers = (error as { response?: { headers?: Record<string, string> } }).response?.headers
      const message =
        (error as { response?: { data?: { message?: string } } }).response?.data?.message ??
        '配置已被他人修改（ETag 冲突）'
      throw new PluginConfigConflictError(message, headers?.etag)
    }
    throw error
  }
}
