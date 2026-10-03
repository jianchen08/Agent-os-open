/**
 * LLM 设置页 query（服务端状态 query 化，OBS-R258-1 四态约定标准入口）
 *
 * - useLlmConfigQuery：LLM 服务配置（providers/models/defaults），重进设置页
 *   缓存秒开；消费方（LlmSettingsPage）以本地可编辑副本承接，写端点返回的
 *   增量切片直接回写副本，不打断编辑
 * - useLlmPresetsQuery：配置面预置声明（llm_service /ext 端点下发，声明驱动
 *   ——前端零硬编码词典）：provider 分组/显示名、常用类型置顶、思考强度档位
 *   白名单；变化频率低，窗口内重挂零请求
 */

import { useQuery } from '@tanstack/react-query'
import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { getLLMConfig, getLLMPresets } from '@/services/api/config'
import { queryKeys } from '@/services/query/queryKeys'

/** LLM 配置新鲜窗口：窗口内重进设置页不重拉 */
const LLM_CONFIG_STALE_TIME = 60_000

/** 预置声明变化频率低：窗口内重挂零请求 */
const LLM_PRESETS_STALE_TIME = 5 * 60_000

/** 思考档位选项随 llm.yaml 热重载/设置页保存变化：窗口内同模型重挂零请求 */
const LLM_THINKING_LEVELS_STALE_TIME = 30_000

/** LLM 服务配置 query（主面，四态消费见 LlmSettingsPage） */
export function useLlmConfigQuery() {
  return useQuery({
    queryKey: queryKeys.llmConfig,
    queryFn: () => getLLMConfig(),
    staleTime: LLM_CONFIG_STALE_TIME,
  })
}

/** 配置面预置声明 query（副面：失败降级由消费方注释声明） */
export function useLlmPresetsQuery() {
  return useQuery({
    queryKey: queryKeys.llmPresets,
    queryFn: () => getLLMPresets(),
    staleTime: LLM_PRESETS_STALE_TIME,
  })
}

/** 思考参数组选项（后端下发：选项 = thinking_strength_params 配置的参数组本身） */
export interface ThinkingLevelOption {
  /** 参数组 JSON 串（紧凑序）——选中即随消息 thinking_strength 透传的线上形态 */
  value: string
  /** 参数渲染标签（如 reasoning_effort=max），真值源在后端 */
  label: string
}

export interface ThinkingLevelsResponse {
  model: string
  /** 参数组选项（厂商级在前、模型级补位，配置顺序）；空 = 未配置（选择器隐藏） */
  options: ThinkingLevelOption[]
  /** 当前参数组：模型 default_params 思考参数命中的选项 value，未匹配为 null */
  current: string | null
}

async function fetchThinkingLevels(modelName: string): Promise<ThinkingLevelsResponse> {
  const response = await apiClient.get<ThinkingLevelsResponse>(
    API_ENDPOINTS.CONFIG.LLM_THINKING_LEVELS,
    { params: { model: modelName } },
  )
  return response.data
}

/** 当前模型思考参数组选项 query（聊天页选择器选项与当前值真值源；模型名变化自动重拉） */
export function useThinkingLevelsQuery(modelName: string | undefined) {
  return useQuery({
    queryKey: [...queryKeys.llmConfig, 'thinking-levels', modelName],
    queryFn: () => fetchThinkingLevels(modelName as string),
    enabled: !!modelName && modelName !== 'unknown',
    staleTime: LLM_THINKING_LEVELS_STALE_TIME,
  })
}
