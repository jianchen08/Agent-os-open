/**
 * 长期任务 API 服务
 *
 * 基于 Task API 实现长期任务功能
 * 使用标签 'long-term' 和 'auto-execute' 来标识长期任务
 *
 * 注意：长期任务创建功能已移除，只能通过 task_submit 工具创建
 */

import { apiClient } from '@/services/api/client'
import type { Task, TaskStatus } from '@/types/task'

/**
 * 长期任务列表响应
 */
export interface LongTermTasksResponse {
  items: Task[]
  total: number
}

/**
 * 任务列表响应结构（匹配后端 list_tasks 返回格式）
 */
interface TaskListApiResponse {
  items: Task[]
  total: number
}

/**
 * 获取长期任务列表
 *
 * 查询带有 'long-term' 标签的任务
 */
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

  const response = await apiClient.get<TaskListApiResponse>(`/api/v1/tasks?${queryParams}`)

  // 从后端 {items, total} 结构中取出任务列表，再客户端侧过滤长期任务
  const allTasks = response.data.items
  const longTermTasks = allTasks.filter((task) => task.tags?.includes('long-term'))

  return {
    items: longTermTasks,
    total: longTermTasks.length,
  }
}

/**
 * 切换自动执行开关
 */
export async function toggleAutoExecute(taskId: string, enabled: boolean): Promise<Task> {
  // 先获取当前任务
  const response = await apiClient.get<Task>(`/api/v1/tasks/${taskId}`)
  const task = response.data

  // 更新标签
  const tags = task.tags || []
  const newTags = enabled
    ? [...tags.filter((t) => t !== 'auto-execute'), 'auto-execute']
    : tags.filter((t) => t !== 'auto-execute')

  const updateResponse = await apiClient.patch<Task>(`/api/v1/tasks/${taskId}`, {
    tags: newTags,
  })

  return updateResponse.data
}

/**
 * 暂停长期任务
 */
export async function pauseLongTermTask(taskId: string): Promise<Task> {
  const response = await apiClient.patch<Task>(`/api/v1/tasks/${taskId}`, {
    status: 'blocked',
  })

  return response.data
}

/**
 * 恢复长期任务
 */
export async function resumeLongTermTask(taskId: string): Promise<Task> {
  const response = await apiClient.patch<Task>(`/api/v1/tasks/${taskId}`, {
    status: 'in_progress',
  })

  return response.data
}

/**
 * 删除长期任务
 */
export async function deleteLongTermTask(taskId: string): Promise<void> {
  await apiClient.delete(`/api/v1/tasks/${taskId}`)
}

/**
 * 将 Task 转换为 Project 格式
 */
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

/**
 * 映射 Task 状态到 Project 状态
 */
function mapTaskStatusToProjectStatus(status: TaskStatus): string {
  const statusMap: Record<TaskStatus, string> = {
    pending: 'planning',
    in_progress: 'running',
    blocked: 'paused',
    completed: 'completed',
    failed: 'failed',
  }

  return statusMap[status] || 'planning'
}
