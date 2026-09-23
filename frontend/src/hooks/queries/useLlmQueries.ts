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
import { getLLMConfig, getLLMPresets } from '@/services/api/config'
import { queryKeys } from '@/services/query/queryKeys'

/** LLM 配置新鲜窗口：窗口内重进设置页不重拉 */
const LLM_CONFIG_STALE_TIME = 60_000

/** 预置声明变化频率低：窗口内重挂零请求 */
const LLM_PRESETS_STALE_TIME = 5 * 60_000

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
