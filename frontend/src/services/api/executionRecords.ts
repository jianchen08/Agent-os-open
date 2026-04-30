/**
 * Execution Records API 服务
 *
 * 提供执行记录的查询和管理接口
 */

import apiClient from '@/services/api/client'

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
  record_count: number
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
 * 执行记录树形结构响应
 */
export interface ExecutionRecordTreeResponse {
  tree: ExecutionRecord[]
  total: number
  session_id: string
  max_depth: number
}

/**
 * 分组概要信息
 */
export interface RecordGroupSummary {
  parent_record_id: string
  record_count: number
  earliest_time: string | null
  first_record?: ExecutionRecord
}

/**
 * 分组概要响应
 */
export interface RecordGroupSummaryResponse {
  groups: RecordGroupSummary[]
  total_groups: number
}

/**
 * 获取执行记录分组概要
 *
 * @param sessionId 会话ID（可选）
 * @returns 分组概要列表
 */
export async function getRecordGroupSummary(
  sessionId?: string,
): Promise<RecordGroupSummaryResponse> {
  const params: Record<string, string> = {}
  if (sessionId) params.session_id = sessionId
  const response = await apiClient.get<RecordGroupSummaryResponse>(
    '/api/v1/execution/records/group-summary',
    { params },
  )
  return response.data
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
  const response = await apiClient.get<ExecutionRecordListResponse>('/api/v1/execution/records', {
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
  const response = await apiClient.get<SessionsListResponse>('/api/v1/execution/records/sessions')
  return response.data
}

/**
 * 获取单个执行记录
 *
 * @param recordId 记录ID
 * @returns 执行记录详情
 */
export async function getExecutionRecord(recordId: string): Promise<ExecutionRecord | null> {
  try {
    const response = await apiClient.get<ExecutionRecord>(`/api/v1/execution/records/${recordId}`)
    return response.data
  } catch (error) {
    console.error('[ExecutionRecordsAPI] 获取执行记录失败:', error)
    return null
  }
}

/**
 * 获取执行记录树（嵌套结构）
 *
 * @param sessionId 会话ID
 * @param maxDepth 最大深度
 * @returns 执行记录树
 */
export async function getExecutionTree(
  sessionId: string,
  maxDepth: number = 5,
): Promise<ExecutionRecordTreeResponse> {
  const response = await apiClient.get<ExecutionRecordTreeResponse>(
    `/api/v1/execution/records/tree/${sessionId}`,
    { params: { max_depth: maxDepth } },
  )
  return response.data
}

/**
 * 获取子执行记录
 *
 * @param parentId 父记录ID
 * @returns 子记录列表
 */
export async function getChildrenRecords(parentId: string): Promise<ExecutionRecord[]> {
  const response = await apiClient.get<ExecutionRecord[]>(
    `/api/v1/execution/records/${parentId}/children`,
  )
  return response.data || []
}

/**
 * 删除执行记录
 *
 * @param recordId 记录ID
 * @returns 是否成功
 */
export async function deleteExecutionRecord(recordId: string): Promise<boolean> {
  try {
    await apiClient.delete(`/api/v1/execution/records/${recordId}`)
    return true
  } catch (error) {
    console.error('[ExecutionRecordsAPI] 删除执行记录失败:', error)
    return false
  }
}

/**
 * 按会话删除执行记录
 *
 * @param sessionId 会话ID
 * @returns 删除的记录数量
 */
export async function deleteExecutionRecordsBySession(sessionId: string): Promise<number> {
  try {
    const response = await apiClient.delete<{ deleted_count: number }>(
      `/api/v1/execution/records/session/${sessionId}`,
    )
    return response.data.deleted_count || 0
  } catch (error) {
    console.error('[ExecutionRecordsAPI] 删除会话执行记录失败:', error)
    return 0
  }
}
