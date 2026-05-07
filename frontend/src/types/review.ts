/**
 * 创意生产编排 - 审批系统类型定义
 *
 * 覆盖审批请求、制品、批注、反馈等完整数据结构
 */

/** 制品类型 */
export type ArtifactType = 'text' | 'image' | 'video' | 'audio' | 'screenshot' | 'file'

/** 制品 —— 一个审批请求可包含多个不同类型的制品 */
export interface Artifact {
  id: string
  type: ArtifactType
  /** 文本内容 或 文件 URL */
  content: string
  title?: string
  metadata?: {
    /** 版本号（返工对比用） */
    version?: number
    /** 上一版内容（diff 对比） */
    previousVersion?: string
    /** 来源软件 */
    source?: string
    timestamp?: string
    mimeType?: string
    /** 视频时长（秒） */
    duration?: number
  }
}

/** 批注类型 */
export type AnnotationType = 'text_selection' | 'image_area' | 'video_timestamp' | 'screenshot_area'

/** 批注 */
export interface Annotation {
  id: string
  type: AnnotationType

  // ---- 文本批注 ----
  selectedText?: string
  textPosition?: { start: number; end: number }

  // ---- 图片 / 截图区域批注 ----
  area?: { x: number; y: number; width: number; height: number }
  imageUrl?: string

  // ---- 视频时间轴批注 ----
  timestamp?: number

  /** 修改建议 */
  suggestion: string
  createdAt: string
}

/** 审批选项 */
export interface ReviewOption {
  id: string
  label: string
  type: 'approve' | 'reject' | 'custom'
}

/** 审批展示模式 */
export type ReviewDisplayMode = 'workspace' | 'floating' | 'fullscreen'

/** 审批请求（后端 → 前端） */
export interface ReviewRequest {
  requestId: string
  title: string
  description: string
  artifacts: Artifact[]
  options: ReviewOption[]
  mode: ReviewDisplayMode
}

/** 审批反馈（前端 → 后端） */
export interface ReviewFeedback {
  requestId: string
  action: 'approve' | 'reject' | 'annotate'
  annotations: Annotation[]
  feedbackText?: string
}

/** 审批状态 */
export type ReviewStatus = 'pending' | 'approved' | 'rejected' | 'in_review'

/** Diff 变更行类型 */
export type DiffLineType = 'unchanged' | 'added' | 'removed'

/** Diff 单行 */
export interface DiffLine {
  type: DiffLineType
  content: string
  lineNumber: number
}

/** Diff 结果 */
export interface DiffResult {
  oldLines: DiffLine[]
  newLines: DiffLine[]
}

/** 外部软件连接器类型 */
export type ConnectorType = 'comfyui' | 'game_engine' | 'video_editor' | 'generic'

/** 外部软件连接状态 */
export type ConnectorStatus = 'disconnected' | 'connecting' | 'connected' | 'error'

/** 外部软件连接器配置 */
export interface ExternalConnector {
  id: string
  type: ConnectorType
  name: string
  status: ConnectorStatus
  config: {
    endpoint?: string
    apiKey?: string
    protocol?: 'http' | 'websocket' | 'mcp'
  }
  capabilities?: string[]
}
