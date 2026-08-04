/**
 * WebSocket相关常量定义
 *
 * 与后端WebSocket端点对齐，确保前后端一致性。
 * Requirements: 4.1, 4.2
 */

import { API_BASE_URL } from './api'

export enum WebSocketStatus {
  DISCONNECTED = 'disconnected',
  CONNECTING = 'connecting',
  CONNECTED = 'connected',
}

/**
 * 从 API_BASE_URL 派生 WebSocket URL
 * http://localhost:8988 -> ws://localhost:8988
 * https://example.com -> wss://example.com
 * 空字符串 -> 从当前页面 location 派生（适用于 Vite 代理模式）
 */
function deriveWsUrl(apiUrl: string): string {
  if (!apiUrl) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}`
  }
  const url = new URL(apiUrl)
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
  return url.origin
}

/**
 * WebSocket服务器URL（从 API_BASE_URL 派生，或从环境变量读取）
 */
export const WS_BASE_URL = import.meta.env.VITE_WS_BASE_URL || deriveWsUrl(API_BASE_URL)

// ---- 协议版本 ----

/**
 * 客户端协议版本号
 *
 * 与后端 PROTOCOL_VERSION 保持一致，用于版本协商。
 */
export const PROTOCOL_VERSION = '3.0.0'

// ---- ACK 配置 ----

/**
 * ACK 确认超时时间（毫秒）
 */
export const WS_ACK_TIMEOUT = 10_000

/**
 * ACK 确认最大重试次数
 */
export const WS_ACK_MAX_RETRIES = 3

/**
 * 需要 ACK 确认的服务端事件类型集合
 */
export const WS_ACK_REQUIRED_EVENTS: ReadonlySet<string> = new Set([
  'interaction_request',
  'approval_required',
  'approval_request',
])

/**
 * 构建全局 WebSocket 连接 URL（不带 thread_id）
 *
 * 用于 GlobalWebSocketService 建立 /ws/chat 全局连接。
 *
 * @param token - JWT访问令牌
 * @returns 完整的 WebSocket URL
 */
export const buildGlobalWebSocketUrl = (token: string): string =>
  `${WS_BASE_URL}/ws/chat?token=${encodeURIComponent(token)}&version=${encodeURIComponent(PROTOCOL_VERSION)}`

/**
 * WebSocket服务端事件类型
 *
 * 对应后端发送的事件类型
 */
export const WS_SERVER_EVENTS = {
  /** 连接确认 */
  CONNECTION_CONFIRMATION: 'connection_confirmation',
  /** 状态变更 */
  STATE_CHANGE: 'state_change',
  /** 需要审批 */
  APPROVAL_REQUIRED: 'approval_required',
  /** 任务完成 */
  TASK_COMPLETED: 'task_completed',
  /** 任务取消 */
  TASK_CANCELLED: 'task_cancelled',
  /** 任务状态实时更新 */
  TASK_STATUS_UPDATE: 'task_status_update',
  /** 任务状态变更（实时推送） */
  TASK_STATUS_CHANGED: 'task_status_changed',
  /** 任务删除 */
  TASK_DELETED: 'task_deleted',
  /** 错误 */
  ERROR: 'error',
  /** 心跳响应（后端发送 heartbeat_ack） */
  HEARTBEAT: 'heartbeat_ack',
  /** 新消息 - 需求 10.3 */
  NEW_MESSAGE: 'new_message',
  /** 节点状态更新 - 需求 10.4 */
  NODE_STATUS_UPDATE: 'node_status_update',
  /** 审批请求 */
  APPROVAL_REQUEST: 'approval_request',
  /** 执行状态更新 - 需求 8.2 */
  EXECUTION_STATUS_UPDATE: 'execution_status_update',
  /** 子 Agent 输入请求 - 需求 1.3 */
  SUB_AGENT_INPUT_REQUEST: 'sub_agent_input_request',
  /** 执行事件（节点执行完成） - 需求 7.2 */
  EXECUTION_EVENT: 'execution_event',
  /** 流式输出开始 */
  STREAM_START: 'stream_start',
  /** 流式输出片段 */
  STREAM_CHUNK: 'stream_chunk',
  /** 流式输出结束 */
  STREAM_END: 'stream_end',
  /** 流式输出错误（LLM 调用失败等） */
  STREAM_ERROR: 'stream_error',
  /** 思考开始 */
  THINKING_START: 'thinking_start',
  /** 思考内容片段 */
  THINKING_CHUNK: 'thinking_chunk',
  /** 思考结束 */
  THINKING_END: 'thinking_end',
  /** 工具调用开始（管道流式事件） */
  TOOL_START: 'tool_start',
  /** 工具调用结果（管道流式事件） */
  TOOL_RESULT: 'tool_result',
  /** 工作流步骤更新 - 需求 3.2 */
  WORKFLOW_STEP_UPDATE: 'workflow_step_update',
  /** 执行开始（统一工具/Agent/工作流执行） */
  EXECUTION_START: 'execution_start',
  /** 执行进度更新 */
  EXECUTION_PROGRESS: 'execution_progress',
  /** 执行完成 */
  EXECUTION_DONE: 'execution_done',
  /** 执行取消 */
  EXECUTION_CANCELLED: 'execution_cancelled',
  /** 执行输出（中间输出） */
  EXECUTION_OUTPUT: 'execution_output',
  /** 执行控制响应 - 需求 5.1, 5.2, 5.3 */
  EXECUTION_CONTROL_RESPONSE: 'execution_control_response',
  /** Agent 消息注入响应 */
  AGENT_INJECT_RESPONSE: 'agent_inject_response',
  /** 人类交互超时提醒 */
  INTERACTION_TIMEOUT_REMINDER: 'interaction_timeout_reminder',
  /** 人类交互请求 */
  INTERACTION_REQUEST: 'interaction_request',
  /** 消息变更 - PostgreSQL LISTEN/NOTIFY */
  MESSAGE_CHANGE: 'MESSAGE_CHANGE',
  /** 子 Agent 创建 - Phase 5 */
  SUB_AGENT_CREATED: 'sub_agent_created',
  /** 子 Agent 等待输入 - Phase 5 */
  SUB_AGENT_WAITING_INPUT: 'sub_agent_waiting_input',
  /** 子 Agent 完成 - Phase 5 */
  SUB_AGENT_COMPLETED: 'sub_agent_completed',
  /** 系统通知（任务完成/失败等，通过统一流式路径发送） */
  SYSTEM_NOTIFICATION: 'system_notification',
  /** Agent 层级变更 - Phase 5 */
  AGENT_LEVEL_CHANGED: 'agent_level_changed',
  /** 消息删除通知 */
  MESSAGE_DELETED: 'message_deleted',
  /** 消息更新通知 */
  MESSAGE_UPDATED: 'message_updated',
  /** Schema 更新（模块 Schema 变更推送） */
  SCHEMA_UPDATED: 'schema_updated',
  /** 迭代开始 */
  ITERATION_START: 'iteration_start',
  /** 迭代结束 */
  ITERATION_END: 'iteration_end',
  /** 会话更新（新建/删除/修改会话时推送） */
  SESSION_UPDATE: 'session_update',
  /** 成本更新（Token 用量变化时推送） */
  COST_UPDATE: 'cost_update',
  /** 流式保活（长时间操作期间由后端发送，防止 chunk 超时） */
  STREAM_KEEPALIVE: 'stream_keepalive',
  /** 管道已接收到消息 */
  PIPELINE_RECEIVED: 'pipeline_received',
  /** 迭代事件（管道引擎迭代开始/结束） */
  ITERATION: 'iteration',
} as const

/**
 * WebSocket客户端消息类型
 *
 * 对应前端发送给后端的消息类型
 */
export const WS_CLIENT_MESSAGES = {
  /** 用户输入 */
  USER_INPUT: 'user_input',
  /** 审批决策 */
  APPROVAL: 'approval',
  /** 心跳 */
  HEARTBEAT: 'heartbeat',
  /** 取消任务 */
  CANCEL: 'cancel',
  /** 用户输入响应（响应子 Agent 的输入请求）- 需求 1.3 */
  USER_INPUT_RESPONSE: 'user_input_response',
  /** 执行控制（暂停/恢复/取消）- 需求 5.1, 5.2, 5.3 */
  EXECUTION_CONTROL: 'execution_control',
  /** 消息 ACK 确认（确认收到关键消息） */
  MESSAGE_ACK: 'message_ack',
} as const

/**
 * 审批决策类型
 */
export const APPROVAL_DECISIONS = {
  /** 批准 */
  APPROVE: 'approve',
  /** 拒绝 */
  REJECT: 'reject',
  /** 修改后批准 */
  MODIFY: 'modify',
} as const

/**
 * WebSocket事件类型
 */
export type WebSocketServerEventType = (typeof WS_SERVER_EVENTS)[keyof typeof WS_SERVER_EVENTS]

export type WebSocketClientMessageType =
  (typeof WS_CLIENT_MESSAGES)[keyof typeof WS_CLIENT_MESSAGES]

export type ApprovalDecisionType = (typeof APPROVAL_DECISIONS)[keyof typeof APPROVAL_DECISIONS]

/**
 * WebSocket心跳配置
 */
export const WS_HEARTBEAT_CONFIG = {
  /** 心跳间隔（毫秒） */
  INTERVAL: 30000,
  /** 心跳超时（毫秒）- 必须大于 INTERVAL，给 ack 留容错，否则 ack 稍慢就误断连 */
  TIMEOUT: 45000,
} as const

/**
 * WebSocket错误码枚举
 *
 * 与后端WebSocket错误码保持一致，用于统一错误处理和重试策略
 */
export enum WebSocketErrorCode {
  // 认证相关 (1000-1999)
  /** 认证失败 */
  AUTH_FAILED = 1001,
  /** 令牌过期 */
  TOKEN_EXPIRED = 1002,
  /** 连接数超限 */
  CONNECTION_LIMIT = 1003,

  // 网络相关 (2000-2999)
  /** 连接丢失 */
  CONNECTION_LOST = 2001,
  /** 连接超时 */
  TIMEOUT = 2002,
  /** 服务端不可达 */
  UNREACHABLE = 2003,

  // 服务端相关 (3000-3999)
  /** 服务端内部错误 */
  SERVER_ERROR = 3001,
  /** 请求频率限制 */
  RATE_LIMITED = 3002,
  /** 服务维护中 */
  MAINTENANCE = 3003,

  // 消息相关 (4000-4999)
  /** 消息过大 */
  MESSAGE_TOO_LARGE = 4001,
  /** 消息格式无效 */
  INVALID_FORMAT = 4002,
  /** 不支持的消息类型 */
  UNSUPPORTED_TYPE = 4003,
  /** 连接被新连接替换（不应重连） */
  CONNECTION_REPLACED = 4004,
}

/**
 * WebSocket消息接口定义
 */

/** 文件附件类型 */
export interface FileAttachment {
  /** 文件ID */
  file_id: string
  /** 原始文件名 */
  filename: string
  /** MIME类型 */
  mime_type: string
  /** 文件类型（image/document）- 可选，服务端可根据 mime_type 推断 */
  file_type?: 'image' | 'document'
  /** Base64编码的文件内容 - 可选，已上传的文件不需要 */
  base64_data?: string
}

/** 用户输入消息 */
export interface UserInputMessage {
  type: typeof WS_CLIENT_MESSAGES.USER_INPUT
  content: string
  /** 文件附件列表（可选） */
  attachments?: FileAttachment[]
  /** 父执行记录 ID（子 Agent 标签发消息时传递） */
  parent_record_id?: string
}

/** 审批消息 */
export interface ApprovalMessage {
  type: typeof WS_CLIENT_MESSAGES.APPROVAL
  decision: ApprovalDecisionType
  reason?: string
  modifications?: Record<string, unknown>
}

/** 心跳消息 */
export interface HeartbeatMessage {
  type: typeof WS_CLIENT_MESSAGES.HEARTBEAT
  timestamp: number
}

/** 取消消息 */
export interface CancelMessage {
  type: typeof WS_CLIENT_MESSAGES.CANCEL
  reason?: string
}

/** 用户输入响应消息（响应子 Agent 的输入请求）- 需求 1.3 */
export interface UserInputResponseMessage {
  type: typeof WS_CLIENT_MESSAGES.USER_INPUT_RESPONSE
  /** 执行 ID */
  execution_id: string
  /** 用户响应内容（与后端 response 字段对应） */
  response: string
}

/** 执行控制消息 - 需求 5.1, 5.2, 5.3 */
export interface ExecutionControlMessage {
  type: typeof WS_CLIENT_MESSAGES.EXECUTION_CONTROL
  /** 执行 ID */
  execution_id: string
  /** 控制动作 */
  action: 'pause' | 'resume' | 'cancel'
  /** 操作原因 */
  reason?: string
}

/** 消息 ACK 确认（前端确认收到关键消息） */
export interface MessageAckMessage {
  type: typeof WS_CLIENT_MESSAGES.MESSAGE_ACK
  /** 被确认的消息 request_id */
  request_id: string
  /** 前端确认收到的时间戳 */
  received_at: string
}

/** 客户端消息联合类型 */
export type WebSocketClientMessage =
  | UserInputMessage
  | ApprovalMessage
  | HeartbeatMessage
  | CancelMessage
  | UserInputResponseMessage
  | ExecutionControlMessage
  | MessageAckMessage

/** 状态变更事件 */
export interface StateChangeEvent {
  type: typeof WS_SERVER_EVENTS.STATE_CHANGE
  previous_state: string
  current_state: string
  thread_id: string
}

/** 审批请求事件 */
export interface ApprovalRequiredEvent {
  type: typeof WS_SERVER_EVENTS.APPROVAL_REQUIRED
  approval_id: string
  content: unknown
  thread_id: string
}

/** 任务完成事件 */
export interface TaskCompletedEvent {
  type: typeof WS_SERVER_EVENTS.TASK_COMPLETED
  result: unknown
  thread_id: string
}

/** 任务取消事件 */
export interface TaskCancelledEvent {
  type: typeof WS_SERVER_EVENTS.TASK_CANCELLED
  reason: string
  thread_id: string
}

/** 错误事件 */
export interface ErrorEvent {
  type: typeof WS_SERVER_EVENTS.ERROR
  error_code: string
  message: string
  thread_id: string
}

/** 连接确认事件 */
export interface ConnectionConfirmationEvent {
  type: typeof WS_SERVER_EVENTS.CONNECTION_CONFIRMATION
  connection_id: string
  thread_id: string
  /** 服务端协商后的协议版本 */
  version?: string
}

/** 工作流步骤更新事件 - 需求 3.2 */
export interface WorkflowStepUpdateEvent {
  type: typeof WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE
  /** 执行 ID */
  execution_id: string
  /** 步骤 ID */
  step_id: string
  /** 步骤名称 */
  step_name: string
  /** 步骤状态 */
  status: string
  /** 步骤输出 */
  output?: Record<string, unknown>
  /** 线程 ID */
  thread_id?: string
}

/** 执行开始事件（统一工具/Agent/工作流） */
export interface ExecutionStartEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_START
  /** 执行 ID */
  execution_id: string
  /** 执行类型 */
  execution_type: 'tool' | 'agent' | 'workflow'
  /** 名称 */
  name: string
  /** 描述 */
  description?: string
  /** 父执行 ID（嵌套时使用） */
  parent_id?: string
  /** 输入参数 */
  input?: Record<string, unknown>
  /** 元数据 */
  metadata?: Record<string, unknown>
  /** 线程 ID */
  thread_id?: string
}

/** 执行进度事件 */
export interface ExecutionProgressEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_PROGRESS
  /** 执行 ID */
  execution_id: string
  /** 进度百分比 (0-100) */
  progress: number
  /** 当前步骤描述 */
  current_step?: string
  /** 进度消息 */
  message?: string
  /** 线程 ID */
  thread_id?: string
}

/** 执行完成事件 */
export interface ExecutionDoneEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_DONE
  /** 执行 ID */
  execution_id: string
  /** 是否成功 */
  success: boolean
  /** 输出结果 */
  output?: Record<string, unknown>
  /** 错误信息 */
  error?: string
  /** 耗时（毫秒） */
  duration_ms?: number
  /** 执行摘要 */
  summary?: string
  /** 线程 ID */
  thread_id?: string
}

/** 执行取消事件 */
export interface ExecutionCancelledEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_CANCELLED
  /** 执行 ID */
  execution_id: string
  /** 取消原因 */
  reason: string
  /** 取消者 */
  cancelled_by?: 'user' | 'system' | 'timeout'
  /** 线程 ID */
  thread_id?: string
}

/** 执行输出事件（中间输出） */
export interface ExecutionOutputEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_OUTPUT
  /** 执行 ID */
  execution_id: string
  /** 新增的输出内容 */
  output: string
  /** true=追加, false=替换 */
  append: boolean
  /** 时间戳 */
  timestamp: string
  /** 线程 ID */
  thread_id?: string
}

/** 执行控制响应事件 - 需求 5.1, 5.2, 5.3 */
export interface ExecutionControlResponseEvent {
  type: typeof WS_SERVER_EVENTS.EXECUTION_CONTROL_RESPONSE
  /** 执行 ID */
  execution_id: string
  /** 执行的动作 */
  action: 'pause' | 'resume' | 'cancel' | 'rollback'
  /** 是否成功 */
  success: boolean
  /** 响应消息 */
  message: string
  /** 新状态 */
  new_status?: 'running' | 'suspended' | 'cancelled' | 'completed' | 'failed'
  /** 线程 ID */
  thread_id?: string
}

/** Agent 消息注入响应事件 */
export interface AgentInjectResponseEvent {
  type: typeof WS_SERVER_EVENTS.AGENT_INJECT_RESPONSE
  /** 执行 ID */
  execution_id: string
  /** Agent ID */
  agent_id: string
  /** 是否成功 */
  success: boolean
  /** 响应消息 */
  message: string
  /** 线程 ID */
  thread_id?: string
}

/** Schema 更新事件（模块 Schema 变更推送） */
export interface SchemaUpdatedEvent {
  type: typeof WS_SERVER_EVENTS.SCHEMA_UPDATED
  module_id: string
  schema_version: string
  changes: string[]
  thread_id?: string
}

/** 迭代开始事件 */
export interface IterationStartEvent {
  type: typeof WS_SERVER_EVENTS.ITERATION_START
  iteration_id: string
  iteration_type: string
  description?: string
  thread_id?: string
}

/** 迭代结束事件 */
export interface IterationEndEvent {
  type: typeof WS_SERVER_EVENTS.ITERATION_END
  iteration_id: string
  success: boolean
  result?: Record<string, unknown>
  thread_id?: string
}

/** 服务端事件联合类型 */
export type WebSocketServerEvent =
  | StateChangeEvent
  | ApprovalRequiredEvent
  | TaskCompletedEvent
  | TaskCancelledEvent
  | ErrorEvent
  | ConnectionConfirmationEvent
  | WorkflowStepUpdateEvent
  | ExecutionStartEvent
  | ExecutionProgressEvent
  | ExecutionDoneEvent
  | ExecutionCancelledEvent
  | ExecutionOutputEvent
  | ExecutionControlResponseEvent
  | AgentInjectResponseEvent
  | SchemaUpdatedEvent
  | IterationStartEvent
  | IterationEndEvent
  | SubAgentCreatedEvent
  | SubAgentWaitingInputEvent
  | SubAgentCompletedEvent
  | AgentLevelChangedEvent
  | MessageDeletedEvent
  | MessageUpdatedEvent

/** 子 Agent 创建事件 - Phase 5 */
export interface SubAgentCreatedEvent {
  type: typeof WS_SERVER_EVENTS.SUB_AGENT_CREATED
  /** 子 Agent ID */
  agentId: string
  /** 子 Agent 名称 */
  agentName: string
  /** Agent 层级 */
  agentLevel: 1 | 2 | 3
  /** 父 Agent ID */
  parentAgentId: string
  /** 关联任务 ID */
  taskId?: string
  /** Agent 路径 */
  path?: string[]
  /** 线程 ID */
  thread_id?: string
  /** 会话 ID */
  sessionId?: string
}

/** 子 Agent 等待输入事件 - Phase 5 */
export interface SubAgentWaitingInputEvent {
  type: typeof WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT
  /** 子 Agent ID */
  agentId: string
  /** 子 Agent 名称 */
  agentName: string
  /** Agent 层级 */
  agentLevel: 1 | 2 | 3
  /** 关联任务 ID */
  taskId?: string
  /** 输入提示 */
  prompt?: string
  /** 线程 ID */
  thread_id?: string
  /** 会话 ID */
  sessionId?: string
}

/** 子 Agent 完成事件 - Phase 5 */
export interface SubAgentCompletedEvent {
  type: typeof WS_SERVER_EVENTS.SUB_AGENT_COMPLETED
  /** 子 Agent ID */
  agentId: string
  /** 子 Agent 名称 */
  agentName: string
  /** Agent 层级 */
  agentLevel: 1 | 2 | 3
  /** 关联任务 ID */
  taskId?: string
  /** 执行结果摘要 */
  summary?: string
  /** 是否成功 */
  success: boolean
  /** 线程 ID */
  thread_id?: string
  /** 会话 ID */
  sessionId?: string
}

/** Agent 层级变更事件 - Phase 5 */
export interface AgentLevelChangedEvent {
  type: typeof WS_SERVER_EVENTS.AGENT_LEVEL_CHANGED
  /** Agent ID */
  agentId: string
  /** 旧层级 */
  oldLevel: 1 | 2 | 3
  /** 新层级 */
  newLevel: 1 | 2 | 3
  /** 变更原因 */
  reason?: string
  /** 线程 ID */
  thread_id?: string
  /** 会话 ID */
  sessionId?: string
}

/** 消息删除事件 */
export interface MessageDeletedEvent {
  type: typeof WS_SERVER_EVENTS.MESSAGE_DELETED
  /** 会话 ID */
  sessionId: string
  /** 消息 ID */
  messageId: string
  /** 删除的消息数量 */
  deletedCount: number
  /** 时间戳 */
  timestamp: string
}

/** 消息更新事件 */
export interface MessageUpdatedEvent {
  type: typeof WS_SERVER_EVENTS.MESSAGE_UPDATED
  /** 会话 ID */
  sessionId: string
  /** 消息 ID */
  messageId: string
  /** 新内容 */
  content?: string
  /** 时间戳 */
  timestamp: string
}
