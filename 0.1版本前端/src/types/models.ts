/**
 * 核心数据模型类型定义
 */

/**
 * Agent 类型
 */
export interface Agent {
  /** Agent 唯一标识 */
  id: string
  /** Agent 配置 ID（用于查找特定 Agent，如 lingxi） */
  configId?: string
  /** Agent 名称 */
  name: string
  /** Agent 描述 */
  description: string
  /** Agent 类型: system | code | doc | test | debug | review */
  type: string
  /** Agent 状态 */
  status: 'active' | 'inactive'
  /** 模型名称（直接访问） */
  model?: string
  /** Agent 配置 */
  config?: {
    /** 模型名称（配置中） */
    model?: string
    /** 系统提示词 */
    system_prompt?: string
    /** 工具名称列表 */
    tool_names?: string[]
    /** 最大迭代次数 */
    max_iterations?: number
    /** 超时时间（秒） */
    timeout?: number
  }
  /** 创建时间 */
  createdAt?: string
  /** 更新时间 */
  updatedAt?: string
}

/**
 * 用户类型
 */
export interface User {
  /** 用户唯一标识 */
  id: string
  /** 用户名 */
  username: string
  /** 邮箱（可选） */
  email?: string
  /** 头像URL（可选） */
  avatar?: string
  /** 创建时间 */
  createdAt: string
}

/**
 * 会话类型
 */
export interface Session {
  /** 会话唯一标识 */
  id: string
  /** 会话标题 */
  title: string
  /** 创建时间 */
  createdAt: string
  /** 更新时间 */
  updatedAt: string
  /** 消息数量 */
  messageCount: number
  /** 会话状态 */
  status?: string
  /** 绑定的 Agent ID - Requirements: 2.1, 3.3 */
  agentId?: string | null
  /** 关联的管道 ID 列表 */
  pipelineIds?: string[]
  /** 当前活跃的管道 ID */
  activePipelineId?: string | null
  /** 元数据 */
  metadata?: Record<string, unknown>
  /** 是否已星标 */
  starred?: boolean
  /** 是否已置顶 */
  pinned?: boolean
}

/**
 * Agent 层级
 */
export type AgentLevel = 1 | 2 | 3

/**
 * 消息角色类型
 */
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool'

/**
 * 思考内容类型
 */
export interface ThinkingContent {
  /** 思考内容 */
  content: string
  /** 是否正在思考中 */
  isThinking: boolean
  /** 思考耗时（毫秒） */
  durationMs?: number
  /** 思考步骤列表（用于流式思考） */
  steps?: ThinkingStep[]
  /** 当前步骤索引 */
  currentStepIndex?: number
}

/**
 * 思考步骤
 */
export interface ThinkingStep {
  /** 步骤ID */
  id: string
  /** 步骤类型 */
  type: 'reasoning' | 'analysis' | 'planning' | 'evaluation'
  /** 步骤内容 */
  content: string
  /** 步骤状态 */
  status: 'pending' | 'running' | 'completed' | 'failed'
  /** 时间戳 */
  timestamp: string
  /** 子步骤 */
  subSteps?: ThinkingStep[]
}

/**
 * 工具调用状态
 */
export type ToolCallStatus = 'pending' | 'running' | 'completed' | 'failed'

/**
 * 工具调用记录（消息级别）
 */
export interface MessageToolCall {
  /** 工具调用唯一标识 */
  call_id: string
  /** 工具名称 */
  tool_name: string
  /** 工具参数 */
  tool_args: Record<string, unknown>
  /** 调用状态 */
  status: ToolCallStatus
  /** 调用结果 */
  result?: unknown
  /** 结构化完整结果数据（后端 tool_result 事件的 result_data），供工具卡片渲染 diff 等 */
  resultData?: unknown
  /** 错误信息 */
  error?: string
  /** 开始时间 */
  started_at?: string
  /** 结束时间 */
  completed_at?: string
  /** 调用耗时（毫秒） */
  duration_ms?: number
  /** 进度百分比 (0-100) - 用于实时进度更新 */
  progress?: number
  /** 中间输出列表（流式追加） */
  partialOutput?: string[]
  /** 预计剩余时间(毫秒) */
  estimatedRemainingMs?: number
  /** 当前执行步骤描述 */
  currentStep?: string
  /** 所属任务容器 ID（用于解析工具卡片的文件路径） */
  containerTaskId?: string
  /**
   * 工具调用开始时消息 content 的长度
   *
   * 用于流式阶段将 content 按 toolCalls 的执行位置分割成多个 text block，
   * 实现文本和工具调用的穿插显示。
   * 仅在流式阶段由前端设置，数据库加载时不会携带此字段。
   */
  _contentLength?: number
  /**
   * 工具调用开始时 thinking.content 的长度
   *
   * 用于流式阶段将 thinking 按 toolCalls 的执行位置分割成多个 thinking block，
   * 实现思考内容和文本/工具调用的穿插显示。
   * 仅在流式阶段由前端设置，数据库加载时不会携带此字段。
   */
  _thinkingLength?: number
}

/**
 * 消息类型（扩展支持任务消息）
 */
export interface Message {
  /** 消息唯一标识 */
  id: string
  /** 所属会话ID */
  sessionId: string
  /** 消息序号（用于排序，从数据库执行记录的 sequence 字段获取） */
  sequence: number
  /** 消息角色 */
  role: MessageRole
  /** 消息内容（完整文本，不包含工具调用标记） */
  content: string
  /** 时间戳 */
  timestamp: string
  /** 生成此消息的 Agent ID（AI 消息可选） */
  agentId?: string | null
  /** 元数据（可选） */
  metadata?: Record<string, unknown>
  /** 附件列表（用户消息可选） */
  attachments?: Array<{
    id?: string
    name: string
    type?: string
    mime_type?: string
    url: string
    size?: number
  }>
  /** 思考内容（AI 消息可选） */
  thinking?: ThinkingContent
  /**
   * 思考内容分割点（流式阶段使用）
   *
   * 流式期间同一个 messageId 的 thinking 包含两轮思考（tool_call 前后各一次），
   * 此字段记录 tool_call 开始时 thinking.content.length，用于拆分 thinking。
   * 第一轮 thinking[0:splitLength] 关联到 tool_call 前的文本，
   * 第二轮 thinking[splitLength:] 关联到 tool_call 后的文本。
   */
  _thinkingSplitLength?: number
  /** 合并前的原始消息 ID 列表（合并连续 assistant 消息时填充） */
  _originalIds?: string[]
  /**
   * 前端最后更新时间戳（ms）
   *
   * 由 updateMessage/finalizeMessage 等本地写入路径维护，记录消息最近一次
   * 在前端被修改的时刻。assistant 消息的「乐观窗口起点」以本字段判定
   * （见 isWithinOptimisticGrace）；user 乐观消息由 addMessage 创建不写此字段，
   * 用 timestamp 判定。
   */
  _lastUpdated?: number
  /** 统一 Part 列表（按 sequence 排序，唯一渲染数据源） */
  parts?: import('./messageParts').MessagePart[]
  /** 消息类型（可选，用于任务消息） */
  messageType?:
    | 'text' // 文本消息（默认）
    | 'task_created' // 任务创建
    | 'task_phase' // 任务阶段变更
    | 'task_ac_update' // 验收标准状态更新
    | 'task_completed' // 任务完成
    | 'task_failed' // 任务失败
  /** 任务 ID（任务消息时提供） */
  taskId?: string
  /** 任务数据（任务消息时提供） */
  taskData?: {
    /** 任务 ID */
    taskId: string
    /** 任务目标 */
    goal?: string
    /** 任务阶段 */
    phase?: 'prepare' | 'execute' | 'evaluate'
    /** 阶段状态 */
    phaseStatus?: string
    /** 验收标准 ID */
    acId?: string
    /** 验收标准是否通过 */
    acPassed?: boolean
    /** 任务结果 */
    result?: unknown
    /** 错误信息 */
    error?: string
  }
  /** 消息状态（流式状态或工具消息状态） */
  status?: 'idle' | 'sending' | 'streaming' | 'completed' | 'error'
  /** 前端乐观消息 ID，用于与服务端持久化消息对账（消除重复/丢失） */
  clientMessageId?: string
  toolCallId?: string
  toolName?: string
  toolArgs?: Record<string, unknown>
  toolResult?: unknown
  toolError?: string
  durationMs?: number
}

/**
 * 消息重试范围
 */
export type RetryScope = 'all' | 'failed_tools' | 'specific_tool'

/**
 * 消息删除范围
 */
export type DeleteScope = 'single' | 'subsequent' | 'related' | 'custom'

/**
 * 审批风险等级
 */
export type RiskLevel = 'low' | 'medium' | 'high'

/**
 * 审批请求类型
 */
export interface ApprovalRequest {
  /** 审批请求唯一标识 */
  id: string
  /** 关联的节点ID */
  nodeId: string
  /** 审批标题 */
  title: string
  /** 审批描述 */
  description: string
  /** 审批上下文 */
  context: {
    /** 任务信息 */
    taskInfo: string
    /** 执行历史 */
    executionHistory: string[]
    /** 风险等级 */
    riskLevel: RiskLevel
  }
  /** 审批数据 */
  data: Record<string, unknown>
  /** 创建时间 */
  createdAt: string
}
