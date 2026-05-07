/**
 * 制品与批注类型定义
 *
 * 定义 Artifact、Annotation 及相关枚举类型，
 * 对应后端 artifacts 模块的数据结构。
 */

/** 制品类型 */
export type ArtifactType =
  | 'text'
  | 'image'
  | 'video'
  | 'code'
  | 'document'
  | 'data'
  | 'composite'

/** 批注目标类型 */
export type AnnotationTarget =
  | 'text_selection'
  | 'image_region'
  | 'video_timeline'
  | 'whole_artifact'

/** 批注状态 */
export type AnnotationStatus = 'active' | 'resolved' | 'dismissed'

/** 制品 */
export interface Artifact {
  id: string
  taskId: string
  title: string
  artifactType: ArtifactType
  content: string
  filePath?: string
  version: number
  parentArtifactId?: string
  metadata: Record<string, any>
  createdAt: string
  updatedAt: string
}

/** 文本选中批注目标 */
export interface TextSelectionTarget {
  startOffset: number
  endOffset: number
  selectedText: string
  startLine?: number
  endLine?: number
}

/** 图片区域批注目标 */
export interface ImageRegionTarget {
  x: number
  y: number
  width: number
  height: number
  label?: string
}

/** 视频时间轴批注目标 */
export interface VideoTimelineTarget {
  startTime: number
  endTime: number
  thumbnailUrl?: string
}

/** 批注目标数据联合类型 */
export type AnnotationTargetData =
  | TextSelectionTarget
  | ImageRegionTarget
  | VideoTimelineTarget
  | Record<string, never>

/** 批注 */
export interface Annotation {
  id: string
  artifactId: string
  targetType: AnnotationTarget
  targetData: AnnotationTargetData
  content: string
  authorType: 'user' | 'agent'
  authorId: string
  status: AnnotationStatus
  createdAt: string
  resolvedAt?: string
}
