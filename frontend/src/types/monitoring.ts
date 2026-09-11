/**
 * 监控相关类型定义
 *
 * 任务列表（monitoring 插件 tasks 视图）的类型面
 */

/**
 * 任务信息（用于列表展示）
 */
export interface TaskInfo {
  /** 任务 ID */
  id: string
  /** 任务名称/意图 */
  intent?: string
  /** 任务名称（后端返回） */
  name?: string
  /** 执行状态 */
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'suspended'
  /** 任务描述 */
  description?: string
  /** 创建时间 */
  created_at: string
  /** 开始时间 */
  started_at?: string
  /** 完成时间 */
  completed_at?: string
  /** Agent ID */
  agent_id?: string
  /** 错误信息 */
  error?: string
  /** 执行时长（毫秒） */
  duration?: number
  /** 当前步骤 */
  current_step?: string
  /** 进度 (0-100) */
  progress?: number
  // ── pipeline_state 派生字段（monitoring 插件 tasks，task = pipeline 单一真值）──
  /** 任务标题（state 派生：task.goal 或 display_name） */
  title?: string
  /** 管道 state 当前阶段（state 真值，与 task.current_step 不同源） */
  current_phase?: string | null
  /** 所属管道 ID */
  pipeline_id?: string
  /** 所属会话（thread）ID */
  thread_id?: string
  /** 关联的任务管理条目 ID（存在时恢复操作走 task_service 插件） */
  task_id?: string | null
  /** 管道消息条数（state.message_count） */
  message_count?: number
  /** 管道累计 token（state.track.total_tokens） */
  total_tokens?: number
  /** 管道是否已结束 */
  ended?: boolean
  /** 数据来源标记（pipeline_state = state 派生） */
  source?: string
}

/** 任务列表响应 */
export interface TaskListResponse {
  items: TaskInfo[]
  total: number
  page: number
  page_size: number
}
