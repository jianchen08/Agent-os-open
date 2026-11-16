/**
 * 消息系统组件类型定义
 */

import type { Message, MessageRole, MessageToolCall, ThinkingContent } from '@/types/models'

/**
 * 消息内容类型
 */

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
 * 发送回调受理协议：回调返回是否受理本次发送。
 * - false = 未受理（管道未就绪/子标签不支持等）：输入侧保留输入与草稿；
 * - true / void = 受理（void 兼容既有只发不回的回调），输入侧清空。
 */
export type SendMessageReceipt = boolean | void

/**
 * 消息发送参数
 */
export interface SendMessageParams {
  /** 消息内容 */
  content: string
  /** 附件列表 */
  attachments?: Attachment[]
  /** 思考强度（参数组 JSON 串，llm_service thinking-levels 选项 value，随消息
   *  透传给后端 llm_core 白名单过滤直覆盖；'' = 不覆盖） */
  thinkingStrength?: string
  /** 显式模型选择（llm.yaml models 键，随消息 model_id 字段透传；内核按
   *  llm_core widget 声明的 frame_field 名单合成 state overlay，llm_core
   *  解析链首位消费=最高优先级覆盖管道配置；'' / 缺省 = 不带字段，跟随配置） */
  modelId?: string
  /** 任务模式（模式体系契约键，开放标签 = registry 模式键；「默认」= 缺席
   *  不带键，经消息级 execution_context.mode 落任务上下文） */
  mode?: string
  /** 子 Tab 发消息时的目标管道 ID，后端直接用它路由 */
  pipelineId?: string
  /** 受理时输入框原文快照（ChatInput→router 内部契约）：随 beginSend 登记，
   *  落定清空按「当前文本 == 快照」比对执行（不吞超时解锁后的新输入） */
  composerText?: string
  /** 受理时草稿键（ChatInput→router 内部契约）：随 beginSend 登记，落定清草稿
   *  经草稿 store 按 key 生效（不依赖 ChatInput 组件存活） */
  draftKey?: string
}

/**
 * 聊天容器属性
 */
export interface ChatContainerProps {
  /** 会话 ID */
  sessionId: string
  /** 是否正在加载 */
  isLoading?: boolean
  /** 发送消息回调；返回是否受理（false=未受理，ChatInput 保留输入与草稿） */
  onSendMessage: (params: SendMessageParams) => SendMessageReceipt
  /** 停止生成回调 */
  onStopGenerate?: () => void
  /** 强制发送队列条目回调（停止当前回复并优先发送该条；参数 = 目标管道 ID + 条目 ID） */
  onForceSendPending?: (pipelineId: string, inputId: string) => void
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
  /** ‹i/n› 多代切换器锚点（消息段模型）：本消息为其锚点覆盖范围的轮末条
   *  assistant 时由 MessageList 标定，值为锚点 base_seq；null/缺省不显示切换器 */
  segmentSwitcherBaseSeq?: number | null
  /** 只读行（压缩段成员原文等冻结副本）：不渲染操作钮——副本的 UI id 无
   *  服务端落点，编辑/删除必然失败，入口直接不给 */
  readOnly?: boolean
  /** 流式总耗时锚点（触发消息发出时刻 ms，MessageList 按最近前置 user 消息
   *  解析）：思考中/执行中指示行据此显示实时总耗时；缺省不显示耗时 */
  streamAnchorMs?: number | null
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
  /** 发送在途（chatInputStore.sendInFlightByPipeline 非空）：已点发送、消息未
   *  落定（气泡认领/队列条出现）——发送按钮转圈、输入锁定；落定或超时/失败撤下
   *  （2026-11-04 落定语义重定义：ack 受理回执不再撤发送态） */
  sendInFlight?: boolean
  /** 执行状态（用于 smart 模式） */
  executionState?: ExecutionState
  /** 发送消息回调；返回是否受理（false=未受理，保留输入与草稿不清空） */
  onSendMessage: (params: SendMessageParams) => SendMessageReceipt
  /** 停止生成回调 */
  onStopGenerate?: () => void
  /** 是否启用文件上传 */
  enableFileUpload?: boolean
  /** 是否启用拖拽上传 */
  enableDragDrop?: boolean
  /** 模型名称（用于文件上传） */
  modelName?: string
  /** 是否启用思考模式切换 */
  enableThinkingMode?: boolean
  /** 当前思考选择（参数组 JSON 串；随消息透传给后端 llm_core 直覆盖） */
  thinkingStrength?: string
  /** 切换思考选择回调（调用方负责标签级记忆） */
  onThinkingStrengthChange?: (strength: string) => void
  /** 当前显式模型选择（llm.yaml models 键；'' = 未选择，选择器占位显示配置模型） */
  modelId?: string
  /** 切换模型选择回调（调用方负责标签级记忆；'' = 清除显式选择回到跟随配置） */
  onModelIdChange?: (modelId: string) => void
  /** 自定义类名 */
  className?: string
  /** 草稿保存的 key（通常是 tabId 或 sessionId），切换 Tab 时保留未发送文本 */
  draftKey?: string
}

export type { Message, MessageRole, MessageToolCall, ThinkingContent }
