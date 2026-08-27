/**
 * Execution Records API 服务
 *
 * 提供执行记录的查询和管理接口
 */

import apiClient from '@/services/api/client'
import { MONITORING_ENDPOINTS } from './endpoints.generated'

/**
 * 执行记录类型
 */
export interface ExecutionRecord {
  id: string
  session_id: string
  parent_record_id?: string
  depth?: number
  sequence?: number
  record_type?: string
  status?: string
  message_data: Record<string, unknown>
  created_at: string
  children?: ExecutionRecord[]
}

/**
 * 会话信息类型
 */
export interface SessionInfo {
  id: string
  title: string
  created_at: string
  updated_at: string
  /** 消息条数（state 摘要缺失时为 null，前端隐藏显示） */
  record_count: number | null
}

/**
 * 执行记录查询参数
 */
export interface ExecutionRecordQuery {
  session_id?: string
  parent_record_id?: string | null
  limit?: number
  offset?: number
}

/**
 * 执行记录列表响应
 */
export interface ExecutionRecordListResponse {
  records: ExecutionRecord[]
  total: number
  session_id?: string
}

/**
 * 会话列表响应
 */
export interface SessionsListResponse {
  sessions: SessionInfo[]
  total: number
}

/**
 * 获取执行记录列表
 *
 * @param params 查询参数
 * @returns 执行记录列表
 */
export async function getExecutionRecords(
  params: ExecutionRecordQuery = {},
): Promise<ExecutionRecordListResponse> {
  const response = await apiClient.get<ExecutionRecordListResponse>(MONITORING_ENDPOINTS.mon_execution_records_list, {
    params,
  })
  return response.data
}

/**
 * 获取有执行记录的会话列表
 *
 * @returns 会话列表
 */
export async function getExecutionRecordsSessions(): Promise<SessionsListResponse> {
  const response = await apiClient.get<SessionsListResponse>(MONITORING_ENDPOINTS.mon_execution_records_sessions)
  return response.data
}

/**
 * @param recordId 记录ID
 * @returns 执行记录详情（端点失败时返回 null）
 */
export async function getExecutionRecord(recordId: string): Promise<ExecutionRecord | null> {
  try {
    const response = await apiClient.get<ExecutionRecord>(MONITORING_ENDPOINTS.mon_execution_record_get.replace('{record_id}', recordId))
    return response.data
  } catch (error) {
    console.error('[ExecutionRecordsAPI] 获取执行记录失败:', error)
    return null
  }
}

export async function getChildrenRecords(parentId: string): Promise<ExecutionRecord[]> {
  const response = await apiClient.get<ExecutionRecord[]>(
    MONITORING_ENDPOINTS.mon_execution_record_children.replace('{record_id}', parentId),
  )
  return response.data || []
}

/**
 * 清空全部执行记录与轨迹响应（内核 9 表 + registry + payload_diag 文件）
 */
export interface ClearAllExecutionRecordsResponse {
  success: boolean
  message: string
  cleared_count: number
  tables?: Record<string, number>
  backup_path?: string | null
  payload_files_deleted?: number
}

/**
 * 清空全部执行记录与轨迹（破坏性操作，需二次确认）
 *
 * 内核侧：VACUUM INTO 自动备份 → 事务清 9 表（users 保留）→ 内存 registry 同清；
 * 插件侧附带清理 logs/payload_diag 快照文件。有运行中管道时后端返回 409
 * （detail 携带提示）。错误不吞——调用方负责展示。
 */
export async function clearAllExecutionRecords(): Promise<ClearAllExecutionRecordsResponse> {
  const response = await apiClient.post<ClearAllExecutionRecordsResponse>(
    MONITORING_ENDPOINTS.mon_execution_records_clear_all,
  )
  return response.data
}
