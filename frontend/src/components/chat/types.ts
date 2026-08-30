/**
 * 消息系统组件类型定义
 */

import type { CumulativeUsage } from '@/stores/contextUsageStore'
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
  /** 思考强度（off/low/medium/high，随消息传给后端 llm_core 路由到模型参数） */
  thinkingStrength?: 'off' | 'low' | 'medium' | 'high'
  /** 子 Tab 发消息时的目标管道 ID，后端直接用它路由 */
  pipelineId?: string
}

/**
 * 聊天容器属性
 */
export interface ChatContainerProps {
  /** 会话 ID */
  sessionId: string
  /** 是否正在加载 */
  isLoading?: boolean
  /** 发送消息回调 */
  onSendMessage: (params: SendMessageParams) => Promise<void>
  /** 停止生成回调 */
  onStopGenerate?: () => void
  /** 自定义类名 */
  className?: string
  /** 是否还有更多历史消息 */
  hasMoreMessages?: boolean
  /** 是否正在加载更多历史消息 */
  isLoadingMoreMessages?: boolean
  /** 加载更多历史消息回调 */
  onLoadMoreMessages?: () => void
  /** 重新生成回调（最后一条 assistant 消息上「重新生成」触发） */
  onRegenerate?: () => void
  /** 回退回调（user 消息「回退」确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
  /** 编辑重发回调（user 消息编辑保存后触发：截断 + 改内容 + 重跑） */
  onEdit?: (messageId: string, newContent: string) => Promise<void> | void
}

/**
 * 消息列表属性
 */
export interface MessageListProps {
  /** 消息列表 */
  messages: Message[]
  /** 是否正在生成回复 */
  isGenerating?: boolean
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
  /** 编辑重发回调（下传给 MessageItem） */
  onEdit?: (messageId: string, newContent: string) => Promise<void> | void
  /** 重新生成回调（最后一条 assistant 消息上「重新生成」触发） */
  onRegenerate?: () => void
  /** 回退回调（user 消息「回退」确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
  /** 待定位消息（sequence 定位键；渲染层消费后调用 onJumpConsumed 清除） */
  jumpTarget?: { sequence: number }
  /** 定位消费完成回调（MessageList 定位成功后清除 uiStore.messageJump） */
  onJumpConsumed?: () => void
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
  /** 模型名称 */
  modelName?: string
  /** 自定义类名 */
  className?: string
  /** 搜索查询（用于高亮显示） */
  searchQuery?: string
  /** 当前 Tab 关联任务 ID（工具卡片打开文件用） */
  taskId?: string
  /** 编辑消息保存回调（消息编辑功能由调用方接入；不传则不显示编辑入口） */
  onEdit?: (messageId: string, newContent: string) => Promise<void> | void
  /** 重新生成回调（最后一条 assistant 消息上「重新生成」触发） */
  onRegenerate?: () => void
  /** 回退回调（user 消息「回退」确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
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
    media_type: string
    size: number
    url: string
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
  /** 当前 Token 使用量（prompt tokens） */
  currentTokenUsage?: number
  /** 最大 Token 限制 */
  maxTokens?: number
  /** 上一轮总 tokens */
  totalTokens?: number
  /** 上一轮输出 tokens（用量浮窗明细） */
  completionTokens?: number
  /** 该管道累计 token 消耗（用量浮窗明细） */
  cumulative?: CumulativeUsage
  /** 是否启用思考模式切换 */
  enableThinkingMode?: boolean
  /** 当前思考强度（off/low/medium/high；随消息传给后端 llm_core 路由模型参数） */
  thinkingStrength?: 'off' | 'low' | 'medium' | 'high'
  /** 切换思考强度回调（调用方负责本地记忆 + 后端覆盖） */
  onThinkingStrengthChange?: (strength: 'off' | 'low' | 'medium' | 'high') => void
  /** 本轮缓存命中 token（悬停 title 详情用） */
  cachedTokens?: number
  /** 本轮缓存命中率 0-1（悬停 title 详情用） */
  hitRatio?: number
  /** 自定义类名 */
  className?: string
  /** 草稿保存的 key（通常是 tabId 或 sessionId），切换 Tab 时保留未发送文本 */
  draftKey?: string
}

export type { Message, MessageRole, MessageToolCall, ThinkingContent }
