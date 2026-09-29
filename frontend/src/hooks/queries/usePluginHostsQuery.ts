/**
 * 插件进程观测 query（监控页「插件」tab 进程观测视图数据源）
 *
 * refetchInterval 10s 对齐内核 invoker 的宿主快照采集周期（proc_state 10s 轮询代采），
 * 页面不可见自动暂停（refetchIntervalInBackground 默认 false）。
 */

import { useQuery } from '@tanstack/react-query'
import { getPluginHosts } from '@/services/api/pluginHosts'
import { queryKeys } from '@/services/query/queryKeys'

/** 插件进程观测轮询间隔（ms）：对齐后端采集周期 */
export const PLUGIN_HOSTS_REFRESH_INTERVAL = 10_000

/** 插件进程观测快照 query hook */
export function usePluginHostsQuery() {
  return useQuery({
    queryKey: queryKeys.pluginHosts,
    // 箭头包裹：隔离 getPluginHosts 的 RetryOptions 可选参
    queryFn: () => getPluginHosts(),
    refetchInterval: PLUGIN_HOSTS_REFRESH_INTERVAL,
  })
}
