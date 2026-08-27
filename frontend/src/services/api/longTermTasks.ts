/** 长期任务 API 服务 基于 Task API 实现长期任务功能 */

import { apiClient } from '@/services/api/client'
import { API_ENDPOINTS } from '@/constants/api'
import type { Task, TaskStatus } from '@/types/task'

export interface LongTermTasksResponse {
  items: Task[]
  total: number
}

/** 任务列表响应结构（匹配后端 list_tasks 返回格式） */
/** 任务列表响应结构（匹配后端 list_tasks 返回格式） */
interface TaskListApiResponse {
  items: Task[]
  total: number
}

/** 获取长期任务列表 查询带有 'long-term' 标签的任务 */
export async function fetchLongTermTasks(params?: {
  status?: TaskStatus
  page?: number
  limit?: number
}): Promise<LongTermTasksResponse> {
  const { status, page = 1, limit = 100 } = params || {}

  const skip = (page - 1) * limit

  // 构建查询参数（仅使用后端支持的参数：status, priority, session_id, limit, offset, skip）
  const queryParams = new URLSearchParams({
    skip: skip.toString(),
    limit: limit.toString(),
  })

  if (status) {
    queryParams.append('status', status)
  }

  const response = await apiClient.get<TaskListApiResponse>(`${API_ENDPOINTS.TASKS.LIST}?${queryParams}`)

  // 从后端 {items, total} 结构中取出任务列表，再客户端侧过滤长期任务
  const allTasks = response.data.items
  const longTermTasks = allTasks.filter((task) => task.tags?.includes('long-term'))

  return {
    items: longTermTasks,
    total: longTermTasks.length,
  }
}

export async function toggleAutoExecute(taskId: string, enabled: boolean): Promise<Task> {
  const response = await apiClient.get<Task>(API_ENDPOINTS.TASKS.GET(taskId))
  const task = response.data

  // 开关用 auto-execute 标签承载：置 enabled 追加标签，否则移除
  const tags = task.tags || []
  const newTags = enabled
    ? [...tags.filter((t) => t !== 'auto-execute'), 'auto-execute']
    : tags.filter((t) => t !== 'auto-execute')

  const updateResponse = await apiClient.patch<Task>(API_ENDPOINTS.TASKS.UPDATE(taskId), {
    tags: newTags,
  })

  return updateResponse.data
}

/** 长期任务的暂停/恢复不走独立端点，直接 PATCH 任务状态：blocked=暂停、running=恢复 */

export async function pauseLongTermTask(taskId: string): Promise<Task> {
  const response = await apiClient.patch<Task>(API_ENDPOINTS.TASKS.UPDATE(taskId), {
    status: 'blocked',
  })

  return response.data
}

/** 恢复长期任务 */
export async function resumeLongTermTask(taskId: string): Promise<Task> {
  const response = await apiClient.patch<Task>(API_ENDPOINTS.TASKS.UPDATE(taskId), {
    status: 'running',
  })

  return response.data
}

export async function cancelLongTermTask(taskId: string, reason?: string): Promise<Partial<Task>> {
  const response = await apiClient.post<Partial<Task>>(API_ENDPOINTS.TASKS.CANCEL(taskId), {
    reason: reason || '用户取消',
  })
  return response.data
}

export function taskToProject(task: Task) {
  return {
    id: task.id,
    userId: task.userId || '',
    sessionId: task.sessionId,
    goal: task.title,
    status: mapTaskStatusToProjectStatus(task.status),
    autoExecute: task.tags?.includes('auto-execute') || false,
    currentTaskIndex: 0,
    tasks: [],
    timestamps: {
      createdAt: task.timestamps?.createdAt || '',
      updatedAt: task.timestamps?.updatedAt || '',
    },
  }
}

function mapTaskStatusToProjectStatus(status: TaskStatus): string {
  const statusMap: Record<string, string> = {
    pending: 'planning',
    running: 'running',
    evaluating: 'running',
    scheduled: 'planning',
    blocked: 'suspended',
    suspended: 'suspended',
    completed: 'completed',
    failed: 'failed',
    cancelled: 'failed',
    timeout: 'failed',
  }

  return statusMap[status] || 'planning'
}
