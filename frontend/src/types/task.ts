/**
 * 任务/长期任务类型定义（Task / Project / AgentTab）
 *
 * 匹配后端数据模型：
 * - Project (projects 表)
 * - Task (tasks 表)
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
  | 'planning'
  | 'running'
  | 'suspended'
  | 'completed'
  | 'failed'

/**
 * 任务状态
 *
 * 单一真值源 = ./taskStatus（tasks 插件 TaskStatus 七态）；
 * 本文件只转出类型，词汇常量/归一/文案函数从 taskStatus 导入。
 */
import type { TaskStatus } from './taskStatus'
export type { TaskStatus } from './taskStatus'

/**
 * 任务类型
 */
export type TaskType =
  // 长期任务三段生命周期：planning = 首个任务，execution = 中间任务，
  // final_evaluation = 末位总体评估任务
  | 'planning'
  | 'execution'
  | 'final_evaluation'

/**
 * Agent 层级
 */
export type AgentLevel = 1 | 2 | 3

/**
 * Agent Tab 状态
 */
export type AgentTabStatus =
  | 'running'
  | 'completed'
  | 'waiting_input'
  | 'failed'
  | 'unknown'

// ============================================
// 核心类型
// ============================================

/**
 * 任务
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
  /** 层级路径（如 ['主管道', '规划Agent']） */
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
// API 响应类型
// ============================================

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
