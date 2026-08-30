/**
 * 监控 API 服务
 *
 * 提供系统监控、任务统计等 API 接口
 *
 * 暴露接口：
 * - getTaskList(page, pageSize, status, options): 任务列表 - 获取任务列表
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type {
  TaskInfo,
  TaskListResponse,
} from '@/types/monitoring'
import type { RetryOptions } from '@/utils/retry'

export async function getTaskList(
  page: number = 1,
  pageSize: number = 20,
  status?: string,
  options: RetryOptions = {},
): Promise<{ items: TaskInfo[]; total: number }> {
  return requestWithRetry(async () => {
    const params: Record<string, string | number> = {
      page,
      page_size: pageSize,
    }
    if (status) {
      params.status = status
    }

    const response = await apiClient.get<TaskListResponse>(API_ENDPOINTS.MONITORING.TASK_LIST, {
      params,
    })
    return {
      items: response.data.items ?? [],
      total: response.data.total ?? 0,
    }
  }, options)
}
