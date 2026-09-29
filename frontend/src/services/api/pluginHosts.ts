/**
 * 插件进程观测 API 服务
 *
 * 只读端点 GET /api/v1/plugins/hosts（admin 鉴权，与既有 admin 端点同法），
 * 监控页「插件」tab 进程观测视图的数据源。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { PluginHostsResponse, PluginRuntimeRow } from '@/types/pluginHosts'
import type { RetryOptions } from '@/utils/retry'

/** 进程观测快照（清单字段兜底为空数组，调用方无需空值分支） */
export interface PluginHostsSnapshot {
  hosts: NonNullable<PluginHostsResponse['hosts']>
  pending_spawn: NonNullable<PluginHostsResponse['pending_spawn']>
}

export async function getPluginHosts(options: RetryOptions = {}): Promise<PluginHostsSnapshot> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<PluginHostsResponse>(API_ENDPOINTS.PLUGINS.HOSTS)
    return {
      hosts: response.data.hosts ?? [],
      pending_spawn: response.data.pending_spawn ?? [],
    }
  }, options)
}

/** 插件运行态表行清单（rows 缺失/null 兜底空数组） */
export async function getPluginRuntimeRows(options: RetryOptions = {}): Promise<PluginRuntimeRow[]> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{ rows?: PluginRuntimeRow[] | null }>(
      API_ENDPOINTS.MONITORING.PLUGIN_RUNTIME,
    )
    return response.data.rows ?? []
  }, options)
}
