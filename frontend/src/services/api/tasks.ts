/**
 * 任务管理 API 服务
 *
 * 提供任务的 CRUD 操作接口
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import type {
  GetProjectsResponse,
  GetTaskPhaseResponse,
  Project,
} from '@/types/task'

/**
 * 任务信息类型
 */
export interface TaskInfo {
  /** 任务 ID */
  id: string
  /** 任务标题 */
  title: string
  /** 任务描述 */
  description?: string
  /** 任务状态 */
  status: string
  /** 任务优先级 */
  priority: string
  /** 任务目标 */
  goal?: Record<string, unknown>
  /** 当前阶段 */
  current_phase?: string
  /** 阶段状态 */
  phase_status?: Record<string, unknown>
  /** 执行者 ID */
  agent_id?: string
  /** 会话线程 ID */
  thread_id?: string
  /** 父任务 ID */
  parent_task_id?: string
  /** 会话 ID */
  session_id?: string
  /** 创建者 ID */
  created_by?: string
  /** 评估指标 IDs */
  evaluation_metric_ids?: string[]
  /** 标签 */
  tags?: string[]
  /** 输入数据 */
  input_data?: Record<string, unknown>
  /** 任务结果 */
  result?: Record<string, unknown>
  /** 错误信息 */
  error_message?: string
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at?: string
  /** 用户 ID */
  user_id?: string
}

/**
 * 任务列表响应
 */
export interface TaskListResponse {
  /** 任务列表 */
  items: TaskInfo[]
  /** 总数量 */
  total: number
}

/**
 * 获取任务列表
 *
 * @param params 查询参数
 * @returns 任务列表
 */
export async function getTasks(params?: {
  skip?: number
  limit?: number
  status?: string
  session_id?: string
}): Promise<TaskListResponse> {
  const response = await apiClient.get<TaskListResponse>(API_ENDPOINTS.TASKS.LIST, { params })
  return response.data
}

/**
 * 删除任务
 *
 * @param id 任务 ID
 * @returns 是否成功
 */
export async function deleteTask(id: string): Promise<boolean> {
  try {
    await apiClient.delete(API_ENDPOINTS.TASKS.DELETE(id))
    return true
  } catch {
    return false
  }
}

// ============================================================================
// 长期任务（项目）API
// ============================================================================

/**
 * 获取项目列表
 *
 * @param params 查询参数
 * @returns 项目列表响应
 */
export async function fetchProjects(params?: {
  page?: number
  limit?: number
  status?: string
}): Promise<GetProjectsResponse> {
  const response = await apiClient.get<GetProjectsResponse>(API_ENDPOINTS.PROJECTS.LIST, { params })
  return response.data
}

/**
 * 创建项目
 *
 * @param goal 项目目标
 * @param sessionId 会话 ID（可选）
 * @param options 其他选项
 * @returns 项目信息
 */
export async function createProject(
  goal: string,
  sessionId?: string,
  options?: {
    autoExecute?: boolean
    metadata?: Record<string, unknown>
  },
): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(API_ENDPOINTS.PROJECTS.CREATE, {
    goal,
    session_id: sessionId,
    auto_execute: options?.autoExecute,
    metadata: options?.metadata,
  })
  return response.data.project
}

/**
 * 手动创建根任务
 *
 * 用户以 L1 身份手动发起一项工作（等价于 L1 主 agent 调 task_submit 提根任务），
 * 为 L2+ 子 agent 提供合法的任务上下文。容器=工作空间集合，非容器=由 target agent 直接执行。
 *
 * @param payload 根任务参数
 * @returns 新创建的任务
 */
export async function createRootTask(payload: {
  title: string
  description?: string
  task_scope: 'container' | 'non_container'
  target_id?: string
  workspace?: string
  /** 工作空间拓扑（与隔离解耦）：worktree（默认）/ plain */
  workspace_mode?: '' | 'worktree' | 'plain'
  isolation_level?: '' | 'isolated' | 'non_isolated'
  inherit?: Record<string, unknown>
  thread_id: string
  parent_task_id?: string
}): Promise<TaskInfo> {
  const response = await apiClient.post<TaskInfo>(API_ENDPOINTS.TASKS.CREATE_ROOT, payload)
  return response.data
}

/**
 * 切换项目自动执行开关
 *
 * @param projectId 项目 ID
 * @param enabled 是否启用
 * @returns 项目信息
 */
export async function toggleProjectAutoExecute(
  projectId: string,
  enabled: boolean,
): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(
    API_ENDPOINTS.PROJECTS.TOGGLE_AUTO_EXECUTE(projectId),
    { enabled },
  )
  return response.data.project
}

/**
 * 暂停项目
 *
 * @param projectId 项目 ID
 * @returns 项目信息
 */
export async function pauseProject(projectId: string): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(
    API_ENDPOINTS.PROJECTS.PAUSE(projectId),
  )
  return response.data.project
}

/**
 * 恢复项目
 *
 * @param projectId 项目 ID
 * @returns 项目信息
 */
export async function resumeProject(projectId: string): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(
    API_ENDPOINTS.PROJECTS.RESUME(projectId),
  )
  return response.data.project
}

// ============================================================================
// 任务阶段 API
// ============================================================================

/**
 * 获取任务阶段状态
 *
 * @param taskId 任务 ID
 * @returns 阶段状态
 */
export async function fetchTaskPhase(taskId: string): Promise<GetTaskPhaseResponse> {
  const response = await apiClient.get<GetTaskPhaseResponse>(
    API_ENDPOINTS.TASK_PHASES.GET_STATUS(taskId),
  )
  return response.data
}

// ============================================================================
// 任务暂停/恢复 API
// ============================================================================

/**
 * 暂停任务操作响应
 */
export interface TaskPauseResumeResponse {
  success: boolean
  task_id: string
  suspended_count?: number
  resumed_count?: number
  message: string
}

/**
 * 暂停任务（级联子任务）
 *
 * @param taskId 任务 ID
 * @returns 操作结果
 */
export async function pauseTask(taskId: string): Promise<TaskPauseResumeResponse> {
  const response = await apiClient.post<TaskPauseResumeResponse>(API_ENDPOINTS.TASKS.PAUSE(taskId))
  return response.data
}

/**
 * 恢复任务（级联子任务）
 *
 * @param taskId 任务 ID
 * @returns 操作结果
 */
export async function resumeTask(taskId: string): Promise<TaskPauseResumeResponse> {
  const response = await apiClient.post<TaskPauseResumeResponse>(API_ENDPOINTS.TASKS.RESUME(taskId))
  return response.data
}

/**
 * 取消任务操作响应
 */
export interface CancelTaskResponse {
  success: boolean
  task_id: string
  cancelled_count?: number
  message: string
}

/**
 * 取消任务
 *
 * @param taskId 任务 ID
 * @returns 操作结果
 */
export async function cancelTask(taskId: string): Promise<CancelTaskResponse> {
  const response = await apiClient.post<CancelTaskResponse>(API_ENDPOINTS.TASKS.CANCEL(taskId))
  return response.data
}
