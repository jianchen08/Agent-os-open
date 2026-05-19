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

/** 制品类型 */
export type ArtifactType =
  | 'text'
  | 'image'
  | 'video'
  | 'audio'
  | 'screenshot'
  | 'file'

/** 图片审阅结果 */
export interface ImageReviewResult {
  isValid: boolean
  format: string
  width: number
  height: number
  aspectRatio: number
  exif: Record<string, any>
  warnings: string[]
  errors: string[]
}

/** 视频审阅结果 */
export interface VideoReviewResult {
  isValid: boolean
  format: string
  durationSeconds: number
  width: number
  height: number
  fps: number
  codec: string
  warnings: string[]
  errors: string[]
}

/** 媒体元数据 */
export interface MediaMetadata {
  type: 'image' | 'video'
  imageResult?: ImageReviewResult
  videoResult?: VideoReviewResult
}

/** 制品 */
export interface Artifact {
  id: string
  type: ArtifactType
  content: string
  title?: string
  metadata?: Record<string, any>
  mediaMetadata?: MediaMetadata
}

/** Diff 行类型 */
export type DiffLineType = 'unchanged' | 'added' | 'removed'

/** Diff 行数据 */
export interface DiffLine {
  type: DiffLineType
  content: string
  lineNumber: number
}

/** 批注类型（用于图片区域标注和视频时间轴标注） */
export interface Annotation {
  id: string
  type: string
  area?: { x: number; y: number; width: number; height: number }
  imageUrl?: string
  timestamp?: number
  suggestion: string
  createdAt: string
}
