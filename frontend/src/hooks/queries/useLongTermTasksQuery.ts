/**
 * 长期任务列表 query（服务端状态 query 化批次 4）
 *
 * longTermTasks 数据唯一真值源 = TanStack Query 缓存（queryKeys.longTermTasks）：
 * - 组件消费走 useLongTermTasksQuery（缓存秒开 + refetchInterval 兜底轮询 +
 *   staleTime 到期后台静默刷新）
 * - 非组件代码（WS 事件 handler / store 编排 action）读写走
 *   readLongTermTasks / updateLongTermTasksCache / invalidateLongTermTasks——
 *   用全局 queryClient 单例，不 new QueryClient
 *
 * 轮询语义（替代原 useTaskPolling 5s setInterval）：
 * - refetchInterval: 5000 —— 兜底轮询频率与旧实现一致
 * - 页面不可见自动暂停（refetchIntervalInBackground 默认 false），等价原
 *   document.hidden 跳过逻辑
 * - 事件驱动路径（task_status_update 等 WS 事件）仍优先走缓存增量写，
 *   轮询仅对账兜底
 */

import { useQuery } from '@tanstack/react-query'
import { fetchLongTermTasks } from '@/services/api/longTermTasks'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import type { Task } from '@/types/task'

/** 长期任务列表兜底轮询间隔（ms）：与旧 useTaskPolling 默认一致 */
export const LONG_TERM_TASKS_REFRESH_INTERVAL = 5_000

/** 获取长期任务列表（queryFn：箭头包裹隔离 fetchLongTermTasks 的可选参） */
async function fetchTasksForQuery(): Promise<Task[]> {
  const response = await fetchLongTermTasks()
  return response.items
}

/** 长期任务列表 query hook：全局活跃订阅 + 5s 兜底轮询。
 *  HomePage 挂载即启用；消费组件（PipelineManagerWidget 等）挂载即订阅。 */
export function useLongTermTasksQuery() {
  return useQuery({
    queryKey: queryKeys.longTermTasks,
    queryFn: fetchTasksForQuery,
    refetchInterval: LONG_TERM_TASKS_REFRESH_INTERVAL,
    // 页面隐藏时暂停轮询（refetchIntervalInBackground 默认 false）——
    // 等价旧 useTaskPolling 的 document.hidden 跳过
  })
}

/** 非组件环境读当前缓存的长期任务列表（无缓存返回空数组，调用方无需空值分支） */
export function readLongTermTasks(): Task[] {
  return queryClient.getQueryData<Task[]>(queryKeys.longTermTasks) ?? []
}

/** 更新缓存的长期任务列表（WS 事件增量/写操作回填共用；无缓存视同 []） */
export function updateLongTermTasksCache(updater: (prev: Task[]) => Task[]): void {
  queryClient.setQueryData<Task[]>(queryKeys.longTermTasks, (prev) => updater(prev ?? []))
}

/** 标记长期任务列表缓存失效（活跃订阅自动重拉；事件路径「先 invalidate 再取」强制新鲜） */
export function invalidateLongTermTasks(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.longTermTasks })
}
