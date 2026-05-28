/**
 * 任务执行闭环系统类型定义
 *
 * 匹配后端数据模型：
 * - Project (projects 表)
 * - Task (tasks 表)
 * - AcceptanceCriterion (acceptance_criteria 表)
 * - TaskPhaseResult (task_phase_results 表)
 *
 * @docs docs/tasks/task-execution-loop-system.md
 */

// ============================================
// 基础类型
// ============================================

/**
 * 项目状态
 */
export type ProjectStatus =
  | 'planning' // 规划中
  | 'running' // 执行中
  | 'suspended' // 已暂停
  | 'completed' // 已完成
  | 'failed' // 已失败

/**
 * 任务状态
 *
 * 与后端 ExecutionStatus 完全对齐，前后端统一字段。
 */
export type TaskStatus =
  | 'pending' // 待执行
  | 'running' // 执行中
  | 'evaluating' // 评估中
  | 'scheduled' // 已调度
  | 'completed' // 已完成
  | 'failed' // 已失败
  | 'blocked' // 已阻塞
  | 'suspended' // 已暂停
  | 'cancelled' // 已取消
  | 'timeout' // 超时

/**
 * 任务类型
 */
export type TaskType =
  | 'planning' // 规划任务（长期任务的第一个任务）
  | 'execution' // 执行任务（长期任务的中间任务）
  | 'final_evaluation' // 总体评估任务（长期任务的最后一个任务）

/**
 * 任务阶段
 */
export type TaskPhase =
  | 'prepare' // 准备阶段
  | 'execute' // 执行阶段
  | 'evaluate' // 评估阶段

/**
 * 阶段状态
 */
export type PhaseStatusType =
  | 'pending' // 待执行
  | 'running' // 执行中
  | 'completed' // 已完成
  | 'failed' // 已失败

/**
 * 验收标准状态
 */
export type ACStatus =
  | 'pending' // 待评估
  | 'evaluating' // 评估中
  | 'passed' // 已通过
  | 'failed' // 未通过

/**
 * 评估器类型
 */
export type EvaluatorType =
  | 'tool' // 工具评估器
  | 'agent' // Agent 评估器
  | 'workflow' // 工作流评估器

/**
 * Agent 层级
 */
export type AgentLevel = 1 | 2 | 3

/**
 * Agent Tab 状态
 */
export type AgentTabStatus =
  | 'running' // 执行中
  | 'completed' // 已完成
  | 'waiting_input' // 等待输入
  | 'failed' // 已失败

// ============================================
// 核心类型
// ============================================

/**
 * 阶段结果
 *
 * 存储任务某个阶段的执行结果
 */
export interface PhaseResult {
  /** 阶段状态 */
  status: PhaseStatusType
  /** 开始时间（ISO 8601 格式） */
  startTime?: string
  /** 结束时间（ISO 8601 格式） */
  endTime?: string
  /** 阶段产物（调研报告、执行计划、子任务列表等） */
  output?: Record<string, any>
  /** 错误信息 */
  error?: string
  /** 执行时长（毫秒） */
  durationMs?: number
}

/**
 * 验收标准
 *
 * 任务的验收标准，包含评估器和评估结果
 */
export interface AcceptanceCriterion {
  /** 验收标准唯一标识 */
  id: string
  /** 所属任务 ID */
  taskId: string
  /** AC 描述 */
  description: string
  /** 评估器类型 */
  evaluatorType: EvaluatorType
  /** 评估器标识（tool_id 或 agent_id 或 workflow_id） */
  evaluatorId: string
  /** 评估状态 */
  status: ACStatus
  /** 评估结果 */
  result?: {
    /** 是否通过 */
    passed: boolean
    /** 评估消息 */
    message: string
    /** 详细信息 */
    details?: any
  }
  /** 评估时间（ISO 8601 格式） */
  evaluatedAt?: string
  /** 重试次数 */
  retryCount?: number
  /** 是否为红线指标（必须通过） */
  isRedLine?: boolean
  /** 权重（用于总体评分） */
  weight?: number
  /** 创建时间（ISO 8601 格式） */
  createdAt?: string
  /** 更新时间（ISO 8601 格式） */
  updatedAt?: string
}

/**
 * 任务
 *
 * 短期任务，包含三阶段执行模型和验收标准
 */
export interface Task {
  /** 任务唯一标识 */
  id: string
  /** 所属长期任务 ID（可空，独立任务则为空） */
  projectId?: string
  /** 父任务 ID（支持任务嵌套） */
  parentTaskId?: string
  /** 关联的执行记录 ID（用于打开 Agent 对话子标签） */
  executionRecordId?: string
  /** 任务标题 */
  title: string
  /** 任务描述 */
  description?: string
  /** 任务目标（包含 title, description, document, context 等） */
  goal?: {
    /** 标题 */
    title?: string
    /** 描述 */
    description?: string
    /** 参考文档 */
    document?: string
    /** 上下文信息 */
    context?: Record<string, any>
  }
  /** 任务状态 */
  status: TaskStatus
  /** 任务类型 */
  taskType?: TaskType
  /** 当前阶段 */
  currentPhase?: TaskPhase
  /** 各阶段状态 */
  phaseStatus?: {
    /** 准备阶段结果 */
    prepare?: PhaseResult
    /** 执行阶段结果 */
    execute?: PhaseResult
    /** 评估阶段结果 */
    evaluate?: PhaseResult
  }
  /** 验收标准列表 */
  acceptanceCriteria?: AcceptanceCriterion[]
  /** Agent 层级 */
  agentLevel?: AgentLevel
  /** 执行者 Agent ID */
  agentId?: string
  /** 会话线程 ID */
  threadId?: string
  /** 所属会话 ID */
  sessionId?: string
  /** 创建者 ID */
  createdBy?: string
  /** 所属用户 ID */
  userId?: string
  /** 输入数据 */
  inputData?: Record<string, any>
  /** 任务结果 */
  result?: Record<string, any>
  /** 目标执行者类型 */
  targetType?: 'agent' | 'workflow' | 'long_term'
  /** 目标执行者 ID */
  targetId?: string
  /** 目标执行者名称 */
  targetName?: string
  /** 任务范围：short_term（短期任务）或 long_term（长期任务） */
  taskScope?: 'short_term' | 'long_term'
  /** 优先级（1-10，数字越大优先级越高） */
  priority?: number
  /** 截止日期（ISO 8601 格式） */
  dueDate?: string
  /** 标签列表 */
  tags?: string[]
  /** 进度统计 */
  progress?: {
    /** 总验收标准数 */
    totalCriteria: number
    /** 已通过验收标准数 */
    passedCriteria: number
    /** 未通过验收标准数 */
    failedCriteria: number
    /** 进度百分比（0-100） */
    progressPercent: number
  }
  /** 重试控制 */
  retry?: {
    /** 当前重试次数 */
    count: number
    /** 最大重试次数 */
    max: number
  }
  /** 时间记录 */
  timestamps?: {
    /** 开始时间（ISO 8601 格式） */
    startedAt?: string
    /** 完成时间（ISO 8601 格式） */
    completedAt?: string
    /** 创建时间（ISO 8601 格式） */
    createdAt: string
    /** 更新时间（ISO 8601 格式） */
    updatedAt: string
  }
  /** 错误信息 */
  errorMessage?: string
  /** 元数据 */
  metadata?: Record<string, any>
  /** 子任务列表 */
  subtasks?: Task[]
}

/**
 * 项目（长期任务）
 *
 * 长期任务，包含多个短期任务
 */
export interface Project {
  /** 项目唯一标识 */
  id: string
  /** 所属用户 ID */
  userId: string
  /** 关联会话 ID */
  sessionId?: string
  /** 长期目标 */
  goal: string
  /** 项目状态 */
  status: ProjectStatus
  /** 自动执行开关 */
  autoExecute: boolean
  /** 当前执行任务索引（从 0 开始） */
  currentTaskIndex: number
  /** 任务列表 */
  tasks?: Task[]
  /** 时间记录 */
  timestamps?: {
    /** 创建时间（ISO 8601 格式） */
    createdAt: string
    /** 更新时间（ISO 8601 格式） */
    updatedAt: string
  }
  /** 元数据 */
  metadata?: Record<string, any>
}

/**
 * Agent Tab
 *
 * Agent 对话标签页
 */
export interface AgentTab {
  /** Tab 唯一标识 */
  id: string
  /** Agent ID */
  agentId: string
  /** Agent 显示名称 */
  agentName: string
  /** Agent 层级 */
  agentLevel: AgentLevel
  /** 关联的任务 ID */
  taskId?: string
  /** 父执行记录 ID（用于过滤子执行记录） */
  parentRecordId?: string
  /** 管道运行实例 ID（用于加载子管道消息） */
  pipelineRunId?: string
  /** 层级路径（如 ['主Agent', '规划Agent']） */
  path: string[]
  /** Tab 状态 */
  status: AgentTabStatus
  /** 是否有未读消息 */
  hasUnread: boolean
  /** 是否可关闭（主 Agent 不可关闭） */
  canClose: boolean
  /** 消息列表 */
  messages?: any[]
}

// ============================================
// 消息类型扩展
// ============================================

/**
 * 任务消息类型
 *
 * 扩展自基础消息类型
 */
export type TaskMessageType =
  | 'text' // 文本消息（现有）
  | 'task_created' // 任务创建
  | 'task_phase' // 任务阶段变更
  | 'task_ac_update' // 验收标准状态更新
  | 'task_completed' // 任务完成
  | 'task_failed' // 任务失败

/**
 * 任务消息数据
 *
 * 包含任务相关的消息数据
 */
export interface TaskMessageData {
  /** 任务 ID */
  taskId: string
  /** 任务目标 */
  goal?: string
  /** 任务阶段 */
  phase?: TaskPhase
  /** 阶段状态 */
  phaseStatus?: string
  /** 验收标准 ID */
  acId?: string
  /** 验收标准是否通过 */
  acPassed?: boolean
  /** 任务结果 */
  result?: any
  /** 错误信息 */
  error?: string
}

// ============================================
// WebSocket 事件类型
// ============================================

/**
 * WebSocket 事件类型
 *
 * 任务相关的 WebSocket 事件
 */
export type TaskWSEventType =
  | 'project_created' // 项目创建
  | 'project_progress' // 项目进度更新
  | 'project_paused' // 项目暂停
  | 'project_resumed' // 项目恢复
  | 'task_created' // 任务创建
  | 'task_phase_changed' // 任务阶段变更
  | 'task_ac_evaluated' // 验收标准评估完成
  | 'task_completed' // 任务完成
  | 'task_failed' // 任务失败
  | 'auto_execute_triggered' // 自动执行触发

/**
 * 项目创建事件
 */
export interface ProjectCreatedEvent {
  eventType: 'project_created'
  projectId: string
  goal: string
  sessionId?: string
}

/**
 * 项目进度更新事件
 */
export interface ProjectProgressEvent {
  eventType: 'project_progress'
  projectId: string
  currentTaskIndex: number
  totalTasks: number
}

/**
 * 项目暂停事件
 */
export interface ProjectPausedEvent {
  eventType: 'project_paused'
  projectId: string
}

/**
 * 项目恢复事件
 */
export interface ProjectResumedEvent {
  eventType: 'project_resumed'
  projectId: string
}

/**
 * 任务创建事件
 */
export interface TaskCreatedEvent {
  eventType: 'task_created'
  taskId: string
  projectId?: string
  goal: string
  taskType: TaskType
  phase: TaskPhase
}

/**
 * 任务阶段变更事件
 */
export interface TaskPhaseChangedEvent {
  eventType: 'task_phase_changed'
  taskId: string
  phase: TaskPhase
  status: PhaseStatusType
  output?: any
}

/**
 * 验收标准评估完成事件
 */
export interface TaskACEvaluatedEvent {
  eventType: 'task_ac_evaluated'
  taskId: string
  acId: string
  passed: boolean
  result?: {
    message: string
    details?: any
  }
}

/**
 * 任务完成事件
 */
export interface TaskCompletedEvent {
  eventType: 'task_completed'
  taskId: string
  projectId?: string
  result?: {
    summary: string
    output?: any
  }
}

/**
 * 任务失败事件
 */
export interface TaskFailedEvent {
  eventType: 'task_failed'
  taskId: string
  projectId?: string
  error: string
  retryCount: number
}

/**
 * 自动执行触发事件
 */
export interface AutoExecuteTriggeredEvent {
  eventType: 'auto_execute_triggered'
  projectId: string
  taskId: string
}

/**
 * L3 子任务类型
 */
export type L3SubtaskType =
  | 'tool_call'
  | 'agent_call'
  | 'workflow_call'
  | 'code_execution'
  | 'file_operation'

/**
 * L3 子任务开始事件
 */
export interface L3SubtaskStartedEvent {
  eventType: 'l3_subtask_started'
  taskId: string
  subtaskId: string
  subtaskType: L3SubtaskType
  name: string
  description?: string
}

/**
 * L3 子任务进度事件
 */
export interface L3SubtaskProgressEvent {
  eventType: 'l3_subtask_progress'
  taskId: string
  subtaskId: string
  progress: number
  message?: string
}

/**
 * L3 子任务完成事件
 */
export interface L3SubtaskCompletedEvent {
  eventType: 'l3_subtask_completed'
  taskId: string
  subtaskId: string
  success: boolean
  result?: Record<string, unknown>
  error?: string
}

/**
 * 需要澄清事件
 */
export interface ClarificationNeededEvent {
  eventType: 'clarification_needed'
  taskId: string
  projectId?: string
  question: string
  context?: Record<string, unknown>
}

/**
 * 任务 WebSocket 事件联合类型
 */
export type TaskWSEvent =
  | ProjectCreatedEvent
  | ProjectProgressEvent
  | ProjectPausedEvent
  | ProjectResumedEvent
  | TaskCreatedEvent
  | TaskPhaseChangedEvent
  | TaskACEvaluatedEvent
  | TaskCompletedEvent
  | TaskFailedEvent
  | AutoExecuteTriggeredEvent
  | L3SubtaskStartedEvent
  | L3SubtaskProgressEvent
  | L3SubtaskCompletedEvent
  | ClarificationNeededEvent

// ============================================
// API 请求/响应类型
// ============================================

/**
 * 创建项目请求
 */
export interface CreateProjectRequest {
  /** 长期目标 */
  goal: string
  /** 关联会话 ID（可选） */
  sessionId?: string
  /** 是否自动执行 */
  autoExecute?: boolean
  /** 元数据 */
  metadata?: Record<string, any>
}

/**
 * 创建项目响应
 */
export interface CreateProjectResponse {
  /** 项目信息 */
  project: Project
}

/**
 * 获取项目列表响应
 *
 * 后端返回结构：{ items: [...], total, limit, offset }
 */
export interface GetProjectsResponse {
  /** 项目列表 */
  items: Project[]
  /** 总数 */
  total: number
  /** 每页数量 */
  limit: number
  /** 偏移量 */
  offset: number
}

/**
 * 获取项目详情响应
 */
export interface GetProjectResponse {
  /** 项目信息（包含任务列表） */
  project: Project
}

/**
 * 切换自动执行请求
 */
export interface ToggleAutoExecuteRequest {
  /** 是否启用自动执行 */
  enabled: boolean
}

/**
 * 切换自动执行响应
 */
export interface ToggleAutoExecuteResponse {
  /** 项目信息 */
  project: Project
}

/**
 * 暂停项目响应
 */
export interface PauseProjectResponse {
  /** 项目信息 */
  project: Project
}

/**
 * 恢复项目响应
 */
export interface ResumeProjectResponse {
  /** 项目信息 */
  project: Project
}

/**
 * 获取任务阶段状态响应
 */
export interface GetTaskPhaseResponse {
  /** 任务 ID */
  taskId: string
  /** 当前阶段 */
  currentPhase: TaskPhase
  /** 各阶段状态 */
  phaseStatus: {
    prepare?: PhaseResult
    execute?: PhaseResult
    evaluate?: PhaseResult
  }
}

/**
 * 完成准备阶段请求
 */
export interface CompletePreparePhaseRequest {
  /** 阶段产物 */
  output?: Record<string, any>
}

/**
 * 完成执行阶段请求
 */
export interface CompleteExecutePhaseRequest {
  /** 阶段产物 */
  output?: Record<string, any>
}

/**
 * 获取阶段产物响应
 */
export interface GetPhaseOutputResponse {
  /** 阶段产物 */
  output?: Record<string, any>
  /** 错误信息 */
  error?: string
}

/**
 * 获取任务验收标准列表响应
 */
export interface GetTaskACsResponse {
  /** 任务 ID */
  taskId: string
  /** 验收标准列表 */
  acceptanceCriteria: AcceptanceCriterion[]
}

/**
 * 评估验收标准请求
 */
export interface EvaluateACRequest {
  /** 评估证据 */
  evidence?: Record<string, any>
}

/**
 * 评估验收标准响应
 */
export interface EvaluateACResponse {
  /** 验收标准信息 */
  acceptanceCriterion: AcceptanceCriterion
}

/**
 * 获取验收标准评估结果响应
 */
export interface GetACResultResponse {
  /** 验收标准信息 */
  acceptanceCriterion: AcceptanceCriterion
}

// ============================================
// UI 相关类型
// ============================================

/**
 * 任务卡片样式
 */
export interface TaskCardStyle {
  /** 是否显示详细信息 */
  showDetails: boolean
  /** 是否显示验收标准 */
  showAcceptanceCriteria: boolean
  /** 是否显示执行图 */
  showExecutionGraph: boolean
}

/**
 * 任务面板折叠状态
 */
export interface TaskPanelState {
  /** 任务面板是否折叠 */
  isCollapsed: boolean
  /** 执行图面板是否折叠 */
  isGraphCollapsed: boolean
}

/**
 * 任务筛选条件
 */
export interface TaskFilter {
  /** 项目 ID */
  projectId?: string
  /** 任务状态 */
  status?: TaskStatus
  /** 任务类型 */
  taskType?: TaskType
  /** Agent 层级 */
  agentLevel?: AgentLevel
  /** 标签 */
  tags?: string[]
  /** 关键词搜索 */
  keyword?: string
}

/**
 * 任务排序方式
 */
export type TaskSortBy =
  | 'created_at' // 创建时间
  | 'updated_at' // 更新时间
  | 'priority' // 优先级
  | 'due_date' // 截止日期
  | 'progress' // 进度

/**
 * 任务排序顺序
 */
export type TaskSortOrder = 'asc' | 'desc'

/**
 * 任务列表查询参数
 */
export interface TaskListQuery {
  /** 分页页码 */
  page?: number
  /** 每页数量 */
  pageSize?: number
  /** 筛选条件 */
  filter?: TaskFilter
  /** 排序字段 */
  sortBy?: TaskSortBy
  /** 排序顺序 */
  sortOrder?: TaskSortOrder
}
