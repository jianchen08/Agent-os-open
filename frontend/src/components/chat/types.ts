/**
 * 消息系统组件类型定义
 */

import type { Message, MessageRole, MessageToolCall, ThinkingContent } from '@/types/models'

/**
 * 消息内容类型
 */
export type MessageContentType = 'text' | 'image' | 'file' | 'audio' | 'code'

/**
 * 附件类型
 */
export interface Attachment {
  /** 附件 ID */
  id: string
  /** 文件名 */
  name: string
  /** 文件类型 */
  type: string
  /** 文件大小（字节） */
  size: number
  /** 文件 URL */
  url?: string
  /** 预览 URL（图片） */
  previewUrl?: string
  /** 上传进度（0-100） */
  progress?: number
  /** 上传状态 */
  status?: 'pending' | 'uploading' | 'completed' | 'failed'
  /** 错误信息 */
  error?: string
}

/**
 * 聊天输入状态
 */
export interface ChatInputState {
  /** 输入文本 */
  text: string
  /** 附件列表 */
  attachments: Attachment[]
  /** 是否正在录音 */
  isRecording: boolean
  /** 是否正在上传 */
  isUploading: boolean
}

/**
 * 消息发送参数
 */
export interface SendMessageParams {
  /** 消息内容 */
  content: string
  /** 附件列表 */
  attachments?: Attachment[]
  /** 是否启用思考模式 */
  enableThinking?: boolean
  /** 子 Tab 发消息时的父执行记录 ID */
  parentRecordId?: string
}

/**
 * 聊天容器属性
 */
export interface ChatContainerProps {
  /** 会话 ID */
  sessionId: string
  /** 消息列表 */
  messages: Message[]
  /** 是否正在加载 */
  isLoading?: boolean
  /** 是否正在生成回复 */
  isGenerating?: boolean
  /** 发送消息回调 */
  onSendMessage: (params: SendMessageParams) => Promise<void>
  /** 停止生成回调 */
  onStopGenerate?: () => void
  /** 重新生成回调 */
  onRegenerate?: (messageId: string) => Promise<void>
  /** 编辑消息回调 */
  onEdit?: (messageId: string, newContent: string) => Promise<void>
  /** 删除消息回调 */
  onDelete?: (messageId: string) => Promise<void>
  /** 当前 Token 使用量 */
  currentTokenUsage?: number
  /** 最大 Token 限制 */
  maxTokens?: number
  /** 模型名称 */
  modelName?: string
  /** 思考模式状态 */
  thinkingMode?: import('@/types/thinkingMode').ThinkingModeState
  /** 思考模式切换回调 */
  toggleThinkingMode?: (enabled: boolean) => Promise<void>
  /** 自定义类名 */
  className?: string
  /** 是否还有更多历史消息 */
  hasMoreMessages?: boolean
  /** 是否正在加载更多历史消息 */
  isLoadingMoreMessages?: boolean
  /** 加载更多历史消息回调 */
  onLoadMoreMessages?: () => void
}

/**
 * 消息列表属性
 */
export interface MessageListProps {
  /** 消息列表 */
  messages: Message[]
  /** 是否正在生成回复 */
  isGenerating?: boolean
  /** 重新生成回调 */
  onRegenerate?: (messageId: string) => Promise<void>
  /** 编辑消息回调 */
  onEdit?: (messageId: string, newContent: string) => Promise<void>
  /** 删除消息回调 */
  onDelete?: (messageId: string) => Promise<void>
  /** 模型名称 */
  modelName?: string
  /** 自定义类名 */
  className?: string
  /** 是否还有更多历史消息 */
  hasMore?: boolean
  /** 是否正在加载更多历史消息 */
  isLoadingMore?: boolean
  /** 加载更多历史消息回调 */
  onLoadMore?: () => void
  /** 会话ID */
  sessionId?: string
  /** 搜索查询（用于高亮显示） */
  searchQuery?: string
}

/**
 * 消息项属性
 */
export interface MessageItemProps {
  /** 消息数据 */
  message: Message
  /** 是否是最后一条消息 */
  isLast?: boolean
  /** 是否正在生成 */
  isGenerating?: boolean
  /** 重新生成回调 */
  onRegenerate?: (messageId: string) => Promise<void>
  /** 编辑消息回调 */
  onEdit?: (messageId: string, newContent: string) => Promise<void>
  /** 删除消息回调 */
  onDelete?: (messageId: string) => Promise<void>
  /** 模型名称 */
  modelName?: string
  /** 自定义类名 */
  className?: string
  /** 搜索查询（用于高亮显示） */
  searchQuery?: string
}

/**
 * 聊天输入模式
 */
export type ChatInputMode = 'full' | 'compact' | 'smart'

/**
 * 执行状态
 */
export type ExecutionState = 'idle' | 'running' | 'paused'

/**
 * 文件上传状态
 */
export type FileUploadStatus = 'pending' | 'uploading' | 'success' | 'error'

/**
 * 待上传文件
 */
export interface PendingFile {
  /** 文件 ID */
  id: string
  /** 文件对象 */
  file: File
  /** 预览 URL（图片） */
  previewUrl?: string
  /** 上传状态 */
  status: FileUploadStatus
  /** 错误信息 */
  error?: string
  /** 上传结果 */
  uploadResult?: {
    file_id: string
    filename: string
    mime_type: string
    file_type: string
    base64_data?: string
  }
}

/**
 * 聊天输入属性（统一版）
 */
export interface ChatInputProps {
  /** 输入模式 */
  mode?: ChatInputMode
  /** 是否禁用 */
  disabled?: boolean
  /** 是否正在生成回复 */
  isGenerating?: boolean
  /** 执行状态（用于 smart 模式） */
  executionState?: ExecutionState
  /** 占位文本 */
  placeholder?: string
  /** 发送消息回调 */
  onSendMessage: (params: SendMessageParams) => void
  /** 停止生成回调 */
  onStopGenerate?: () => void
  /** 是否启用文件上传 */
  enableFileUpload?: boolean
  /** 是否启用拖拽上传 */
  enableDragDrop?: boolean
  /** 模型名称（用于文件上传） */
  modelName?: string
  /** 当前 Token 使用量 */
  currentTokenUsage?: number
  /** 最大 Token 限制 */
  maxTokens?: number
  /** 是否启用思考模式切换 */
  enableThinkingMode?: boolean
  /** 思考模式状态 */
  thinkingMode?: import('@/types/thinkingMode').ThinkingModeState
  /** 思考模式切换回调 */
  toggleThinkingMode?: (enabled: boolean) => Promise<void>
  /** 自定义类名 */
  className?: string
}

export type { Message, MessageRole, MessageToolCall, ThinkingContent }
