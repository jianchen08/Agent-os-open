/** 管道运行快照 API 服务（统一管道管理数据源，内核 `/api/v1/pipelines/runs`） */

import { apiClient } from '@/services/api/client'
import { API_ENDPOINTS } from '@/constants/api'
import type { PipelineRunInfo, PipelineStatus } from '@/types/pipeline'

/** 管道运行快照响应 */
export interface PipelineRunsResponse {
  items: PipelineRunInfo[]
}

/** 获取管道运行快照（按开始时间倒序；status 可选过滤） */
export async function fetchPipelineRuns(params?: {
  status?: PipelineStatus
  limit?: number
}): Promise<PipelineRunInfo[]> {
  const query = new URLSearchParams()
  if (params?.status) {
    query.append('status', params.status)
  }
  if (params?.limit) {
    query.append('limit', String(params.limit))
  }
  const qs = query.toString()
  const response = await apiClient.get<PipelineRunsResponse>(
    `${API_ENDPOINTS.PIPELINES.RUNS}${qs ? `?${qs}` : ''}`,
  )
  return response.data.items ?? []
}
