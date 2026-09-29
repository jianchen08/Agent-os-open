/**
 * 插件运行态表 query（进程观测卡片的 join 源：上次崩溃时间戳）
 *
 * refetchInterval 30s 对齐原「插件运行」表声明轮询周期（该表已下架，数据迁入
 * 卡片）。页面不可见自动暂停（refetchIntervalInBackground 默认 false）。
 */

import { useQuery } from '@tanstack/react-query'
import { getPluginRuntimeRows } from '@/services/api/pluginHosts'
import { queryKeys } from '@/services/query/queryKeys'

/** 运行态表轮询间隔（ms）：对齐原运行表声明刷新周期 */
export const PLUGIN_RUNTIME_REFRESH_INTERVAL = 30_000

/** 插件运行态行清单 query hook */
export function usePluginRuntimeQuery() {
  return useQuery({
    queryKey: queryKeys.pluginRuntime,
    queryFn: () => getPluginRuntimeRows(),
    refetchInterval: PLUGIN_RUNTIME_REFRESH_INTERVAL,
  })
}
