/**
 * 审批类型定义
 *
 * 定义 ReviewRequest、ReviewFeedback 及相关枚举类型，
 * 对应后端 review 模块的数据结构。
 */

/** 审批状态 */
export type ReviewStatus =
  | 'pending'
  | 'in_review'
  | 'approved'
  | 'rejected'
  | 'partially_approved'
  | 'cancelled'
  | 'timeout'

/** 审批请求 */
export interface ReviewRequest {
  id: string
  taskId: string
  threadId: string
  sessionId: string
  tabId: string
  title: string
  description: string
  artifactIds: string[]
  status: ReviewStatus
  priority: 'low' | 'normal' | 'high' | 'critical'
  timeoutSeconds: number
  createdAt: string
  updatedAt: string
  reviewedAt?: string
  completedAt?: string
  metadata: Record<string, any>
}

/** 审批反馈 */
export interface ReviewFeedback {
  id: string
  reviewRequestId: string
  responseType: 'approved' | 'denied' | 'answered' | 'timeout' | 'cancelled'
  overallComment: string
  annotations: ReviewFeedbackAnnotation[]
  userId?: string
  createdAt: string
}

/** 审批反馈中的批注项 */
export interface ReviewFeedbackAnnotation {
  artifactId: string
  targetType: string
  targetData: Record<string, any>
  content: string
}
