/**
 * 会话 Token 使用 API 服务
 *
 * 提供获取会话上下文 Token 使用量的精确统计功能，支持基于父执行记录 ID 获取精确的上下文 token 使用量，支持获取会话的 Token 总量统计
 *
 * 暴露接口：
 * - getSessionTotalTokenUsage(sessionId): SessionTokenUsageResponse - 获取会话的 Token 总量统计
 * - getContextTokenUsage(sessionId, parentExecutionRecordId): ContextTokenUsageResponse - 获取会话的上下文 Token 使用量
 * - SessionTokenUsageResponse - 会话 Token 用量响应类型
 * - ContextTokenUsageResponse - 上下文 Token 使用量响应类型
 */

import apiClient from '@/services/api/client'

/**
 * 会话 Token 用量响应类型
 */
export interface SessionTokenUsageResponse {
  /** 会话 ID */
  session_id: string
  /** 总 Token 数量 */
  total_tokens: number
  /** Prompt Token 数量 */
  prompt_tokens: number
  /** Completion Token 数量 */
  completion_tokens: number
  /** 请求次数 */
  request_count: number
}

/**
 * 上下文 Token 使用量响应类型
 */
export interface ContextTokenUsageResponse {
  /** 当前上下文 Token 数量 */
  current_context_tokens: number
  /** 是否为估算值 */
  is_estimated: boolean
  /** 使用的模型 */
  model: string
  /** 兼容字段：总 Token 数 */
  total_tokens?: number
  /** 兼容字段：模型名称 */
  model_name?: string
  /** 兼容字段：上下文窗口 */
  context_window?: number
}

export async function getSessionTotalTokenUsage(
  sessionId: string,
): Promise<SessionTokenUsageResponse> {
  const response = await apiClient.get<SessionTokenUsageResponse>(
    `/api/v1/sessions/${sessionId}/total-token-usage`,
  )
  return response.data
}

export async function getContextTokenUsage(
  sessionId: string,
  parentExecutionRecordId?: string,
): Promise<ContextTokenUsageResponse> {
  const response = await apiClient.get<ContextTokenUsageResponse>(
    `/api/v1/sessions/${sessionId}/context-token-usage`,
    {
      params: parentExecutionRecordId
        ? {
            parent_execution_record_id: parentExecutionRecordId,
          }
        : undefined,
    },
  )
  return response.data
}
