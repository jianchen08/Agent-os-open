/**
 * Agent 列表 query（服务端状态 query 化）
 *
 * agents 数据唯一真值源 = TanStack Query 缓存（queryKeys.agents）。
 * 原实现走 agentStore 裸 fetch（手写 Authorization 头、无缓存、isLoading 防重），
 * 本 hook 迁移到 apiClient 统一传输层（token 自动注入 + 401 刷新 + 退避重试），
 * 响应映射/按 config_id 去重逻辑从 agentStore 原样平移。
 * agentStore 仅保留 currentAgentId（UI 选择态）。
 */

import { useQuery } from '@tanstack/react-query'
import { getAgents, type AgentListResponse } from '@/services/api/agents'
import { ErrorType, reportError } from '@/services/errorReporting'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import type { Agent } from '@/types/models'

/** Agent 目录变化频率低（改配置才变），新鲜窗口放宽到 5 分钟 */
const AGENTS_STALE_TIME = 5 * 60_000

/** 后端 snake_case 响应 → 前端 Agent 映射 + 按 config_id 去重
 * （内核 agents_handler 遍历 config/agents/** 下所有 YAML 不去重——
 *  如 main/agentos.yaml 与 main_agent.yaml 的 config_id 均为 agentos，
 *  相同 config_id 只保留第一个，避免 SessionEditModal 渲染 option 时 key 重复） */
function mapAndDedupeAgents(data: AgentListResponse): Agent[] {
  // 宽松视图：后端响应含类型未声明的 config_id 字段，按 Record 宽容消费
  const rawItems = (data.items ?? []) as unknown as Array<Record<string, unknown>>
  const mapped = rawItems.map(
    (agent): Agent => ({
      id: (agent.id as string) || (agent.config_id as string),
      configId: agent.config_id as string | undefined,
      name: agent.name as string,
      description: (agent.description as string) || '',
      type: (agent.agent_type as Agent['type']) || 'atomic',
      status: (agent.status as Agent['status']) || 'active',
      model: agent.model as string | undefined,
      config: {
        model: agent.model as string | undefined,
        system_prompt: agent.system_prompt as string | undefined,
        tool_names: agent.tool_names as string[] | undefined,
        max_iterations: agent.max_iterations as number | undefined,
        timeout: agent.timeout as number | undefined,
      },
      createdAt: agent.created_at as string | undefined,
      updatedAt: agent.updated_at as string | undefined,
    }),
  )

  const seenConfigIds = new Set<string>()
  return mapped.filter((agent) => {
    const key = agent.configId || agent.id
    if (!key) return true
    if (seenConfigIds.has(key)) return false
    seenConfigIds.add(key)
    return true
  })
}

async function fetchAgentsMapped(): Promise<Agent[]> {
  try {
    const data = await getAgents()
    return mapAndDedupeAgents(data)
  } catch (error) {
    const errorMsg = error instanceof Error ? error.message : '获取 Agent 列表失败'
    reportError(errorMsg, {
      type: ErrorType.SERVER,
      componentName: 'useAgentsQuery',
      operation: 'fetchAgents',
    })
    throw error
  }
}

export function useAgentsQuery() {
  return useQuery({
    queryKey: queryKeys.agents,
    queryFn: fetchAgentsMapped,
    staleTime: AGENTS_STALE_TIME,
  })
}

/** 非组件环境读当前缓存的 agent 列表（无缓存返回空数组） */
export function readAgents(): Agent[] {
  return queryClient.getQueryData<Agent[]>(queryKeys.agents) ?? []
}
