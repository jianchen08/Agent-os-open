/**
 * 插件状态聚合 query（服务端状态 query 化，OBS-R258-1 四态约定标准入口）
 *
 * 单一缓存 = TanStack Query 缓存（queryKeys.plugins）：插件启停后由
 * PluginsSettingsPage toggle 乐观更新缓存，重进设置页缓存秒开。
 * 聚合三面：插件状态（主面）+ 工具能力（/api/v1/schema tools 面）+
 * 契约状态（ADR 2026-08-28 净化标示）；副面读失败降级空，不阻断插件列表。
 */

import { useQuery } from '@tanstack/react-query'
import { parseContractStatus, type PluginContractStatus } from '@/components/debug/ContractStatusPanel'
import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { queryKeys } from '@/services/query/queryKeys'

/** 插件状态信息（对齐后端 plugins_status_handler 返回） */
export interface PluginStatus {
  plugin_id: string
  name: string
  description?: string | null
  config_type: string
  host_type: string
  version: string | null
  enabled: boolean
  activation: string
  status: string
  config_files: Array<{ id: string; label: string; path: string }>
  has_contributes: boolean
  has_http_endpoints: boolean
  /** 准入分级（2026-09-25）：被剥除的危险前端能力（host_js/host_css），
   *  非空 = 设置页显示"已限制"（未授予即注册面剥除，禁静默） */
  restricted_capabilities?: string[]
  error: string | null
}

/** 工具能力条目（/api/v1/schema 的 tools 面，ToolDescriptor 序列化子集） */
export interface ToolCapability {
  name: string
  description?: string
  plugin_id?: string
  category?: string
  source?: string
  input_schema?: Record<string, unknown>
}

export interface PluginsAggregate {
  plugins: PluginStatus[]
  capabilities: ToolCapability[]
  contract: PluginContractStatus[]
}

export function usePluginsQuery() {
  return useQuery({
    queryKey: queryKeys.plugins,
    queryFn: async (): Promise<PluginsAggregate> => {
      const res = await apiClient.get<PluginStatus[]>(API_ENDPOINTS.PLUGINS.LIST)
      // 能力面与插件面同拉（读失败不阻断插件列表——能力区降级空）
      let tools: ToolCapability[] = []
      try {
        const schema = await apiClient.get<{ tools?: ToolCapability[] }>(API_ENDPOINTS.SCHEMA.GET)
        tools = Array.isArray(schema.data?.tools) ? schema.data.tools : []
      } catch {
        // 副面降级：能力区显示为空，不伪装插件列表失败
        tools = []
      }
      // 契约状态面（ADR 2026-08-28：插件行内"已净化/工具被剔除"标示的数据源；
      // 读失败不阻断插件列表——标示降级为不显示）
      let contract: PluginContractStatus[] = []
      try {
        const cs = await apiClient.get<unknown>(API_ENDPOINTS.PLUGINS.CONTRACT_STATUS)
        contract = parseContractStatus(cs.data)
      } catch {
        // 副面降级：净化标示不显示
        contract = []
      }
      return { plugins: res.data, capabilities: tools, contract }
    },
    staleTime: 60_000,
  })
}
