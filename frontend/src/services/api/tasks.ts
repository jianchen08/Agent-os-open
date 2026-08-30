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

export interface TaskInfo {
  id: string
  title: string
  description?: string
  status: string
  priority: string
  goal?: Record<string, unknown>
  current_phase?: string
  phase_status?: Record<string, unknown>
  agent_id?: string
  thread_id?: string
  parent_task_id?: string
  session_id?: string
  created_by?: string
  evaluation_metric_ids?: string[]
  tags?: string[]
  input_data?: Record<string, unknown>
  result?: Record<string, unknown>
  error_message?: string
  created_at: string
  updated_at?: string
  user_id?: string
}

export interface TaskListResponse {
  items: TaskInfo[]
  total: number
}

export async function getTasks(params?: {
  skip?: number
  limit?: number
  status?: string
  session_id?: string
}): Promise<TaskListResponse> {
  const response = await apiClient.get<TaskListResponse>(API_ENDPOINTS.TASKS.LIST, { params })
  return response.data
}

/** @returns 删除成功与否 */
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

export async function fetchProjects(params?: {
  page?: number
  limit?: number
  status?: string
}): Promise<GetProjectsResponse> {
  const response = await apiClient.get<GetProjectsResponse>(API_ENDPOINTS.PROJECTS.LIST, { params })
  return response.data
}

export async function createProject(
  goal: string,
  sessionId?: string,
  options?: {
    autoExecute?: boolean
    metadata?: Record<string, unknown>
    /** 项目文件夹（显式目录；缺省自动生成 {工作空间}/projects/<标题>） */
    path?: string
  },
): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(API_ENDPOINTS.PROJECTS.CREATE, {
    goal,
    session_id: sessionId,
    auto_execute: options?.autoExecute,
    metadata: options?.metadata,
    path: options?.path,
  })
  return response.data.project
}

/**
 * 手动创建根任务
 *
 * 用户以 L1 身份手动发起一项工作（等价于 L1 主 agent 调 task_submit 提根任务），
 * 为 L2+ 子 agent 提供合法的任务上下文；project_id 挂靠项目（= 文件夹+登记，
 * 项目创建与任务解耦：新建项目走 createProject 独立入口，本接口只挂靠）。
 *
 * @param payload 根任务参数
 * @returns 新创建的任务
 */
export async function createRootTask(payload: {
  title: string
  description?: string
  /** 挂靠项目 id（可选，任务在项目文件夹下执行） */
  project_id?: string
  target_id?: string
  workspace?: string
  /** 工作空间拓扑（与隔离解耦）：worktree（默认）/ plain */
  workspace_mode?: '' | 'worktree' | 'plain'
  isolation_level?: '' | 'isolated' | 'non_isolated'
  inherit?: Record<string, unknown>
  thread_id: string
}): Promise<TaskInfo> {
  const response = await apiClient.post<TaskInfo>(API_ENDPOINTS.TASKS.CREATE_ROOT, payload)
  return response.data
}

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

export async function pauseProject(projectId: string): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(
    API_ENDPOINTS.PROJECTS.PAUSE(projectId),
  )
  return response.data.project
}

export async function resumeProject(projectId: string): Promise<Project> {
  const response = await apiClient.post<{ project: Project }>(
    API_ENDPOINTS.PROJECTS.RESUME(projectId),
  )
  return response.data.project
}

// ============================================================================
// 任务阶段 API
// ============================================================================

export async function fetchTaskPhase(taskId: string): Promise<GetTaskPhaseResponse> {
  const response = await apiClient.get<GetTaskPhaseResponse>(
    API_ENDPOINTS.TASK_PHASES.GET_STATUS(taskId),
  )
  return response.data
}

// ============================================================================
// 任务暂停/恢复 API
// ============================================================================

export interface TaskPauseResumeResponse {
  success: boolean
  task_id: string
  suspended_count?: number
  resumed_count?: number
  message: string
}

/** 暂停任务（级联子任务） */
export async function pauseTask(taskId: string): Promise<TaskPauseResumeResponse> {
  const response = await apiClient.post<TaskPauseResumeResponse>(API_ENDPOINTS.TASKS.PAUSE(taskId))
  return response.data
}

/** 恢复任务（级联子任务） */
export async function resumeTask(taskId: string): Promise<TaskPauseResumeResponse> {
  const response = await apiClient.post<TaskPauseResumeResponse>(API_ENDPOINTS.TASKS.RESUME(taskId))
  return response.data
}

export interface CancelTaskResponse {
  success: boolean
  task_id: string
  cancelled_count?: number
  message: string
}

export async function cancelTask(taskId: string): Promise<CancelTaskResponse> {
  const response = await apiClient.post<CancelTaskResponse>(API_ENDPOINTS.TASKS.CANCEL(taskId))
  return response.data
}
