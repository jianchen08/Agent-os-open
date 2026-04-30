/**
 * Agent 执行类型定义
 */

/** 执行状态 */
export type ExecutionStatus = 'pending' | 'running' | 'completed' | 'failed' | 'paused'

/** 执行记录类型 */
export type ExecutionRecordType =
  | 'user_message' // 用户消息
  | 'agent_response' // Agent 回复（content 可包含 [[exec:ID]] 标记）
  | 'tool_call' // 工具调用（元数据）
  | 'task_execution' // 任务执行（元数据，包含 todos）

/** 执行记录 */
export interface ExecutionRecord {
  /** 记录 ID */
  id: string
  /** 会话 ID */
  session_id: string
  /** 父记录 ID（下层记录上层） */
  parent_record_id?: string
  /** 记录类型 */
  record_type: ExecutionRecordType
  /** 执行器类型 */
  executor_type?: 'tool' | 'agent'
  /** 执行器 ID */
  executor_id?: string
  /** 执行器名称 */
  executor_name?: string
  /** 是否可交互 */
  is_interactive?: boolean
  /** 文本内容（可包含 [[exec:ID]] 标记） */
  content?: string
  /** 输入数据 */
  input_data?: Record<string, unknown>
  /** 输出数据 */
  output_data?: Record<string, unknown>
  /** 状态 */
  status: ExecutionStatus
  /** 错误信息 */
  error_message?: string
  /** 开始时间 */
  started_at?: string
  /** 完成时间 */
  completed_at?: string
  /** 耗时（毫秒） */
  duration_ms?: number
  /** 嵌套深度 */
  depth: number
  /** 创建时间 */
  created_at: string
}

/** TODO 项 */
export interface TodoItem {
  /** TODO ID */
  id: string
  /** 内容 */
  content: string
  /** 状态 */
  status: 'pending' | 'in_progress' | 'completed' | 'failed'
  /** 依赖的其他 TODO ID */
  depends_on?: string[]
  /** 优先级 */
  priority?: 'high' | 'medium' | 'low'
}

/** Agent 执行 */
export interface AgentExecution {
  id: string
  session_id: string
  status: ExecutionStatus
  current_step?: string
  created_at: string
  updated_at: string
}

/** 执行跟踪项 */
export interface ExecutionTraceItem {
  timestamp: string
  type: 'step' | 'result' | 'error'
  content: string
}

/** 评估结果 */
export interface EvaluationResult {
  score: number
  feedback: string
  passed: boolean
}

/** 执行详情 */
export interface ExecutionDetails {
  execution: AgentExecution
  traces: ExecutionTraceItem[]
  evaluation?: EvaluationResult
}

/** 执行摘要 */
export interface ExecutionSummary {
  id: string
  session_id: string
  status: ExecutionStatus
  created_at: string
  updated_at: string
}

/** 执行列表响应 */
export interface ExecutionListResponse {
  executions: ExecutionSummary[]
  total: number
}

/** 用户输入请求 */
export interface UserInputRequest {
  execution_id: string
  input: string
}

/** 执行状态更新消息 */
export interface ExecutionStatusUpdateMessage {
  execution_id: string
  status: ExecutionStatus
  current_step?: string
}

/** 子 Agent 输入请求消息 */
export interface SubAgentInputRequestMessage {
  parent_execution_id: string
  sub_agent_id: string
  input: string
}
