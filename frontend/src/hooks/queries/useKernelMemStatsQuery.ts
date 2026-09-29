/**
 * 内核 memstats query（监控页全口径内存的内核段数据源）
 *
 * refetchInterval 10s 对齐插件宿主快照轮询周期，页面不可见自动暂停
 * （refetchIntervalInBackground 默认 false）。
 */

import { useQuery } from '@tanstack/react-query'
import { getKernelMemStats } from '@/services/api/systemMetrics'
import { queryKeys } from '@/services/query/queryKeys'

/** 内核 memstats 轮询间隔（ms）：对齐插件段刷新节奏 */
export const KERNEL_MEMSTATS_REFRESH_INTERVAL = 10_000

/** 内核 memstats 快照 query hook */
export function useKernelMemStatsQuery() {
  return useQuery({
    queryKey: queryKeys.kernelMemStats,
    // 箭头包裹：隔离 getKernelMemStats 的 RetryOptions 可选参
    queryFn: () => getKernelMemStats(),
    refetchInterval: KERNEL_MEMSTATS_REFRESH_INTERVAL,
  })
}
