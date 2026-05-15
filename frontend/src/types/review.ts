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

/** 差异行类型 */
export type DiffLineType = 'unchanged' | 'added' | 'removed'

/** 差异行 */
export interface DiffLine {
  type: DiffLineType
  content: string
  lineNumber: number
}

/** 批注类型 */
export type AnnotationType = 'image_area' | 'video_timestamp' | 'text_selection'

/** 图片区域定义 */
export interface AnnotationArea {
  x: number
  y: number
  width: number
  height: number
}

/** 审批批注 */
export interface Annotation {
  id: string
  type: AnnotationType
  area?: AnnotationArea
  timestamp?: number
  imageUrl?: string
  suggestion: string
  createdAt: string
}
