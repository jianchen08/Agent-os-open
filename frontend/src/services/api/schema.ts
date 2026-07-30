/**
 * Schema 聚合 API 服务
 *
 * 对接后端 /api/v1/schema 端点，获取插件能力清单和 UI Schema 聚合数据。
 * 后端 schema_handler 输出 agents/pipelines/tools/routes，其中每个 agent/pipeline
 * 携带插件声明的 ui_schema（见 kernel/crates/api/src/routes.rs）。
 *
 * @module api/schema
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'
import type { RenderingSpaceType } from '@/types/schema'

/** 插件声明的单个 UI Widget 配置 */
export interface PluginUiWidget {
  /** Widget 实例标识（插件内唯一） */
  id: string
  /** Widget 类型标识，对应 WidgetRegistry 的注册 key */
  type: string
  /** 目标渲染空间 */
  space?: RenderingSpaceType
  /** 触发时机（如 on_route_signal:wait） */
  trigger?: string
  /** 组件属性 */
  props?: Record<string, unknown>
}

/** 插件 ui_schema 结构 */
export interface PluginUiSchema {
  /** 插件声明的 Widget 列表 */
  widgets?: PluginUiWidget[]
}

/** Schema 端点返回的单个 agent/pipeline 条目（含 ui_schema） */
export interface SchemaEntry {
  id: string
  name: string
  version: string
  role?: string
  /** 插件声明的前端 UI Schema（可能为 null/undefined） */
  ui_schema?: PluginUiSchema | null
}

/** /api/v1/schema 聚合响应（与后端 SchemaResponse 对齐） */
export interface SchemaResponse {
  agents: SchemaEntry[]
  pipelines: SchemaEntry[]
  tools: Array<Record<string, unknown>>
  routes: Record<string, unknown>
  /** 声明了 config_files 的插件（配置面板数据源） */
  plugin_configs: Array<{
    plugin_id: string
    plugin_name: string
    config_files: Array<{ id: string; path: string; label: string }>
  }>
  /** 声明了 contributes 的插件（贡献点数据源，内核原样透传 contributes 结构） */
  plugin_contributes: Array<{
    plugin_id: string
    plugin_name: string
    contributes: Record<string, unknown[]>
  }>
}

/**
 * 获取聚合 Schema（含插件 ui_schema 声明）
 *
 * @param options - 重试选项
 * @returns Schema 聚合响应
 */
export async function getSchema(options: RetryOptions = {}): Promise<SchemaResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<SchemaResponse>(API_ENDPOINTS.SCHEMA.GET)
    return response.data
  }, options)
}

/**
 * 从插件 ui_schema 提取要渲染的 Widget 列表
 *
 * 约定 ui_schema 格式：
 * `{ "widgets": [{ "id": "review_panel", "type": "review_document", "space": "workspace", "trigger": "on_route_signal:wait", "props": {...} }] }`
 *
 * ui_schema 缺失或不含 widgets 时返回空数组（静默降级为"该插件无前端 Widget 声明"）。
 *
 * @param pluginUiSchema - 插件声明的 ui_schema（可能为 null/undefined）
 * @returns 要渲染的 Widget 列表
 */
export function getWidgetsForPlugin(pluginUiSchema: PluginUiSchema | null | undefined): PluginUiWidget[] {
  if (!pluginUiSchema || !Array.isArray(pluginUiSchema.widgets)) return []
  return pluginUiSchema.widgets
}
