/**
 * 触发器 API 服务
 *
 * 暴露接口：
 * - listTriggers(params): 列出所有触发器
 * - getTriggerStats(): 获取触发器统计信息
 * - getTrigger(triggerId): 获取单个触发器详情
 * - createTrigger(request): 创建触发器
 * - updateTrigger(triggerId, request): 更新触发器
 * - deleteTrigger(triggerId): 删除触发器
 * - enableTrigger(triggerId): 启用触发器
 * - disableTrigger(triggerId): 禁用触发器
 * - manualTrigger(triggerId, request): 手动触发触发器
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface TriggerResponse {
  /** 触发器 ID */
  id: string
  /** 触发器名称 */
  name: string
  /** 触发器类型 */
  trigger_type: string
  /** 是否启用 */
  enabled: boolean
  /** 执行次数 */
  execution_count: number
  /** 最后执行时间 */
  last_execution?: string
  /** 最后执行结果 */
  last_result?: Record<string, any>
  /** 触发器配置 */
  config: Record<string, any>
}

export interface TriggerListResponse {
  /** 总数 */
  total: number
  /** 触发器列表 */
  triggers: TriggerResponse[]
}

export interface TriggerStatsResponse {
  /** 总触发器数 */
  total_triggers: number
  /** 已启用的触发器数 */
  enabled_triggers: number
  /** 已禁用的触发器数 */
  disabled_triggers: number
  /** 按类型统计 */
  type_counts: Record<string, number>
  /** 触发器 ID 列表 */
  trigger_ids: string[]
}

export interface TriggerCreateRequest {
  /** 触发器 ID */
  id: string
  /** 触发器名称 */
  name: string
  /** 触发器类型: time/event/condition */
  trigger_type: string
  /** 是否启用 */
  enabled?: boolean
  /** 触发器描述 */
  description?: string
  /** 动作列表 */
  actions?: Record<string, any>[]
  /** 元数据 */
  metadata?: Record<string, any>
  /** 时间调度配置 */
  schedule?: Record<string, any>
  /** 事件配置 */
  event?: Record<string, any>
  /** 条件配置 */
  condition?: Record<string, any>
}

export interface TriggerUpdateRequest {
  /** 触发器名称 */
  name?: string
  /** 是否启用 */
  enabled?: boolean
  /** 触发器描述 */
  description?: string
  /** 动作列表 */
  actions?: Record<string, any>[]
  /** 元数据 */
  metadata?: Record<string, any>
  /** 时间调度配置 */
  schedule?: Record<string, any>
  /** 事件配置 */
  event?: Record<string, any>
  /** 条件配置 */
  condition?: Record<string, any>
}

export interface ManualTriggerRequest {
  /** 触发上下文 */
  context?: Record<string, any>
}

export interface TriggerOperationResult {
  /** 操作状态 */
  status: string
  /** 触发器 ID */
  id: string
}

export interface ManualTriggerResult {
  /** 操作状态 */
  status: string
  /** 触发器 ID */
  trigger_id: string
  /** 执行结果 */
  result?: Record<string, any>
}

export interface ListTriggersParams {
  /** 只返回已启用的触发器 */
  enabled_only?: boolean
  /** 过滤触发器类型 */
  trigger_type?: string
}

export async function listTriggers(
  params?: ListTriggersParams
): Promise<TriggerListResponse> {
  const response = await apiClient.get<TriggerListResponse>(
    API_ENDPOINTS.TRIGGERS.LIST,
    { params }
  )
  return response.data
}

export async function getTriggerStats(): Promise<TriggerStatsResponse> {
  const response = await apiClient.get<TriggerStatsResponse>(
    API_ENDPOINTS.TRIGGERS.STATS
  )
  return response.data
}

export async function getTrigger(triggerId: string): Promise<TriggerResponse> {
  const response = await apiClient.get<TriggerResponse>(
    API_ENDPOINTS.TRIGGERS.GET(triggerId)
  )
  return response.data
}

export async function createTrigger(
  request: TriggerCreateRequest
): Promise<TriggerOperationResult> {
  const response = await apiClient.post<TriggerOperationResult>(
    API_ENDPOINTS.TRIGGERS.CREATE,
    request
  )
  return response.data
}

export async function updateTrigger(
  triggerId: string,
  request: TriggerUpdateRequest
): Promise<TriggerOperationResult> {
  const response = await apiClient.put<TriggerOperationResult>(
    API_ENDPOINTS.TRIGGERS.UPDATE(triggerId),
    request
  )
  return response.data
}

export async function deleteTrigger(
  triggerId: string
): Promise<TriggerOperationResult> {
  const response = await apiClient.delete<TriggerOperationResult>(
    API_ENDPOINTS.TRIGGERS.DELETE(triggerId)
  )
  return response.data
}

export async function enableTrigger(
  triggerId: string
): Promise<TriggerOperationResult> {
  const response = await apiClient.post<TriggerOperationResult>(
    API_ENDPOINTS.TRIGGERS.ENABLE(triggerId)
  )
  return response.data
}

export async function disableTrigger(
  triggerId: string
): Promise<TriggerOperationResult> {
  const response = await apiClient.post<TriggerOperationResult>(
    API_ENDPOINTS.TRIGGERS.DISABLE(triggerId)
  )
  return response.data
}

export async function manualTrigger(
  triggerId: string,
  request?: ManualTriggerRequest
): Promise<ManualTriggerResult> {
  const response = await apiClient.post<ManualTriggerResult>(
    API_ENDPOINTS.TRIGGERS.TRIGGER(triggerId),
    request
  )
  return response.data
}

export const triggersApi = {
  listTriggers,
  getTriggerStats,
  getTrigger,
  createTrigger,
  updateTrigger,
  deleteTrigger,
  enableTrigger,
  disableTrigger,
  manualTrigger,
}
