/**
 * 应用壳进程树内存指标 query（监控页全口径内存的应用壳段数据源）
 *
 * 数据走 Electron IPC（window.electronAPI.appMetrics.get），仅 Electron
 * 形态启用（Web 下 electronAPI 缺失 → enabled false，汇总条该段显 —）；
 * refetchInterval 10s 对齐插件段刷新节奏，页面不可见自动暂停。
 */

import { useQuery } from '@tanstack/react-query'
import { queryKeys } from '@/services/query/queryKeys'

/** 应用壳指标轮询间隔（ms）：对齐插件段刷新节奏 */
export const APP_METRICS_REFRESH_INTERVAL = 10_000

/** 应用壳进程树内存指标 query hook */
export function useAppMetricsQuery() {
  const appMetricsApi = window.electronAPI?.appMetrics
  return useQuery({
    queryKey: queryKeys.appMetrics,
    // enabled 守卫 appMetricsApi 非空（与 useDebugQueries 的 pipelineId 断言同款）
    queryFn: () => (appMetricsApi as NonNullable<typeof appMetricsApi>).get(),
    refetchInterval: APP_METRICS_REFRESH_INTERVAL,
    enabled: appMetricsApi != null,
  })
}
