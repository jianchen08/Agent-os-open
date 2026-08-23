/**
 * 管道管理面板全量任务列表 query（服务端状态 query 化批次 4）
 *
 * PipelineManagerWidget 原先自拉 `GET /ext/task_service/tasks`（skip=0, limit=100，
 * 不过滤 long-term）并用本地 30s setInterval 轮询。端点与 fetchLongTermTasks
 * 相同，但语义不同：后者客户端过滤 long-term 标签（queryKeys.longTermTasks），
 * 而本 query 保留全量任务（任务节点/任务管道判定权威源），故独立 key
 * （queryKeys.pipelineAllTasks），不与长期任务列表合流。
 *
 * 轮询语义：refetchInterval 30s（与管道注册表同频，等价原本地 interval）+
 * refetchOnWindowFocus（全局默认）替代原 interval 无条件拉取。
 */

import { useQuery } from '@tanstack/react-query'
import { TASK_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import apiClient from '@/services/api/client'
import { queryKeys } from '@/services/query/queryKeys'

/** 任务列表响应结构（task_service 插件 list_tasks 返回格式） */
interface TaskListApiResponse {
  items: Record<string, unknown>[]
  total: number
}

/** 全量任务拉取间隔（ms）：与管道注册表轮询同频 */
export const PIPELINE_ALL_TASKS_INTERVAL = 30_000

/** queryFn：拉取全量任务列表（不过滤 long-term） */
async function fetchAllTasks(): Promise<Record<string, unknown>[]> {
  const resp = await apiClient.get<TaskListApiResponse>(TASK_SERVICE_ENDPOINTS.tasks_list, {
    params: { skip: 0, limit: 100 },
  })
  return resp.data.items ?? []
}

/** 管道管理面板全量任务列表 query：挂载即拉取 + 30s 轮询 */
export function useAllTasksQuery() {
  return useQuery({
    queryKey: queryKeys.pipelineAllTasks,
    queryFn: fetchAllTasks,
    refetchInterval: PIPELINE_ALL_TASKS_INTERVAL,
  })
}
