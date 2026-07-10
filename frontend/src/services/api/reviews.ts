/**
 * 审批 API 调用
 *
 * 封装审批请求相关的 REST API 请求。
 */

import { apiClient } from './client'

const BASE = '/api/v1/reviews'

/** 创建审批请求 */
export async function createReview(params: {
  taskId: string
  threadId: string
  sessionId: string
  tabId: string
  title: string
  description?: string
  artifactIds?: string[]
  priority?: string
  timeoutSeconds?: number
  metadata?: Record<string, any>
}): Promise<any> {
  return apiClient.post(BASE, {
    task_id: params.taskId,
    thread_id: params.threadId,
    session_id: params.sessionId,
    tab_id: params.tabId,
    title: params.title,
    description: params.description ?? '',
    artifact_ids: params.artifactIds,
    priority: params.priority ?? 'normal',
    timeout_seconds: params.timeoutSeconds,
    metadata: params.metadata,
  })
}

/** 获取审批详情 */
export async function getReview(reviewId: string): Promise<any> {
  return apiClient.get(`${BASE}/${reviewId}`)
}

/** 获取任务的审批列表 */
export async function listReviewsByTask(taskId: string, limit = 50): Promise<any> {
  return apiClient.get(`${BASE}?task_id=${encodeURIComponent(taskId)}&limit=${limit}`)
}

/** 提交审批反馈 */
export async function submitFeedback(reviewId: string, params: {
  responseType: string
  overallComment?: string
  annotations?: Array<{
    artifactId: string
    targetType: string
    targetData: Record<string, any>
    content: string
  }>
  userId?: string
}): Promise<any> {
  return apiClient.post(`${BASE}/${reviewId}/feedback`, {
    response_type: params.responseType,
    overall_comment: params.overallComment ?? '',
    annotations: params.annotations?.map((a) => ({
      artifact_id: a.artifactId,
      target_type: a.targetType,
      target_data: a.targetData,
      content: a.content,
    })),
    user_id: params.userId,
  })
}

/** 标记已查看 */
export async function markAsViewed(reviewId: string): Promise<any> {
  return apiClient.post(`${BASE}/${reviewId}/viewed`, {})
}

/** 取消审批 */
export async function cancelReview(reviewId: string, reason?: string): Promise<any> {
  return apiClient.post(`${BASE}/${reviewId}/cancel`, { reason })
}
