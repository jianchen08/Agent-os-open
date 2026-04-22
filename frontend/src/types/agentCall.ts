/**
 * Agent 调用记录相关类型定义
 *
 * 与后端 /api/v1/agent-calls/* 端点对齐
 */

/**
 * Agent 调用状态枚举
 */
export enum AgentCallStatus {
  /** 等待中 */
  PENDING = 'pending',
  /** 执行中 */
  RUNNING = 'running',
  /** 已完成 */
  COMPLETED = 'completed',
  /** 已失败 */
  FAILED = 'failed',
  /** 已取消 */
  CANCELLED = 'cancelled',
}

/**
 * Agent 调用者层级
 */
export enum CallerLevel {
  /** L1 Agent */
  L1 = 'L1',
  /** L2 Agent */
  L2 = 'L2',
}

/**
 * Agent 调用操作类型
 */
export enum OperationType {
  /** 聊天 */
  CHAT = 'chat',
  /** 工具调用 */
  TOOL_CALL = 'tool_call',
  /** 任务执行 */
  TASK_EXECUTE = 'task_execute',
  /** 其他 */
  OTHER = 'other',
}

/**
 * Agent 调用记录响应类型
 */
export interface AgentCallRecord {
  /** 记录 ID */
  id: string
  /** 执行 ID */
  execution_id: string
  /** 调用者层级 */
  caller_level: string
  /** 目标 Agent ID */
  target_agent_id: string
  /** 目标 Agent 名称 */
  target_agent_name: string
  /** 操作类型 */
  operation_type: string
  /** 指令摘要 */
  instruction_summary: string
  /** 状态 */
  status: string
  /** 是否成功 */
  success?: boolean
  /** 结果摘要 */
  result_summary?: string
  /** 错误信息 */
  error?: string
  /** 开始时间 */
  start_time?: string
  /** 结束时间 */
  end_time?: string
  /** 执行时长（秒） */
  duration?: number
}

/**
 * Agent 调用记录详情响应类型
 */
export interface AgentCallRecordDetail extends AgentCallRecord {
  /** 完整指令 */
  instruction: string
  /** 上下文 */
  context?: Record<string, any>
  /** 执行结果 */
  result?: Record<string, any>
  /** 超时时间 */
  timeout: number
  /** 重试次数 */
  retry_count: number
  /** 优先级 */
  priority: string
  /** 创建时间 */
  created_at: string
}

/**
 * Agent 调用记录列表响应类型
 */
export interface AgentCallListResponse {
  /** 记录列表 */
  records: AgentCallRecord[]
  /** 总数 */
  total: number
  /** 每页数量 */
  limit: number
  /** 偏移量 */
  offset: number
}

/**
 * Agent 调用统计响应类型
 */
export interface AgentCallStatistics {
  /** 总调用次数 */
  total: number
  /** 按状态统计 */
  by_status: Record<string, number>
  /** 按调用者层级统计 */
  by_caller_level: Record<string, number>
  /** 按操作类型统计 */
  by_operation_type: Record<string, number>
  /** 成功率 (%) */
  success_rate: number
  /** 平均执行时长（秒） */
  avg_duration: number
}

/**
 * 查询 Agent 调用记录列表的参数
 */
export interface ListAgentCallsParams {
  /** 执行 ID */
  execution_id?: string
  /** 目标 Agent ID */
  target_agent_id?: string
  /** 调用者层级 (L1/L2) */
  caller_level?: string
  /** 状态 */
  status?: string
  /** 操作类型 */
  operation_type?: string
  /** 开始时间（范围查询） */
  start_time?: string
  /** 结束时间（范围查询） */
  end_time?: string
  /** 返回数量 */
  limit?: number
  /** 偏移量 */
  offset?: number
}

/**
 * 获取 Agent 调用统计的参数
 */
export interface GetAgentCallStatisticsParams {
  /** 目标 Agent ID */
  target_agent_id?: string
  /** 开始时间 */
  start_time?: string
  /** 结束时间 */
  end_time?: string
}
