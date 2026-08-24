/**
 * 聚合 schema query（服务端状态 query 化批次 2）
 *
 * schema 是全站核心数据源（agents/pipelines/tools/contributes），此前各消费端
 * 独立调 getSchema 无共享缓存（设置中枢/GrowthLoop 等各自裸拉）。
 * 本 query 收敛为单一缓存：进入设置页签秒开、staleTime 窗口内跨页零请求。
 * schema_updated / resync_required 事件路径走 invalidateSchemaCache 强制新鲜。
 */

import { useQuery } from '@tanstack/react-query'
import { getSchema } from '@/services/api/schema'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

/** schema 变化频率低（插件启停/热重载才变，事件驱动主动失效），窗口放宽 5 分钟 */
const SCHEMA_STALE_TIME = 5 * 60_000

export function useSchemaQuery() {
  return useQuery({
    queryKey: queryKeys.schema,
    // 箭头包裹：隔离 getSchema 的 RetryOptions 可选参
    queryFn: () => getSchema(),
    staleTime: SCHEMA_STALE_TIME,
  })
}

/** 非组件环境取 schema（GrowthLoop 装载链用）：缓存新鲜直读，过期拉取一次 */
export async function fetchSchemaCached() {
  return queryClient.fetchQuery({
    queryKey: queryKeys.schema,
    queryFn: () => getSchema(),
    staleTime: SCHEMA_STALE_TIME,
  })
}

/** 事件驱动强制失效（schema_updated / resync_required / 插件启停后）：
 *  下一次 fetchSchemaCached 或活跃 useSchemaQuery 订阅必发新请求 */
export function invalidateSchemaCache(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: queryKeys.schema })
}
