/**
 * 思考强度 ↔ 管道模型参数映射工具
 *
 * 正向（用户选强度 → 参数）：STRENGTH_TO_PARAMS（types/thinkingMode.ts），
 * 随 user_input 透传给后端 llm_core 路由。
 *
 * 反向（管道参数 → 强度）：新会话/切换管道标签时，用户未显式设置过强度时，
 * 从对应管道模型的 default_params 反向推断强度档位（mapParamsToStrength），
 * 保证输入框显示与管道实际生效参数一致。
 */

import type { ModelConfig } from '@/services/api/config'
import type { ThinkingStrength } from '@/types/thinkingMode'

interface StrengthHintParams {
  reasoning_effort?: string
  temperature?: number
  thinking?: { type?: string }
}

/**
 * 管道模型参数 → 思考强度（无法判断返回 null，调用方回退默认档）。
 *
 * 推断优先级：
 * 1. reasoning_effort（low/medium/high/max → 档位，max 视为 high）
 * 2. thinking.type === 'disabled' → off（显式关闭思考）
 * 3. temperature >= 0.7 → low；<= 0.4 → high（effort 缺失时的近似）
 * 4. 其余 → null
 */
export function mapParamsToStrength(params: StrengthHintParams | undefined): ThinkingStrength | null {
  if (!params) return null

  const effort = params.reasoning_effort
  if (effort === 'low' || effort === 'medium' || effort === 'high') return effort
  if (effort === 'max') return 'high'

  if (params.thinking?.type === 'disabled') return 'off'

  if (typeof params.temperature === 'number') {
    if (params.temperature >= 0.7) return 'low'
    if (params.temperature <= 0.4) return 'high'
  }

  return null
}

/**
 * 从 LLM 配置（models: model_id → ModelConfig）中定位某模型名的 default_params。
 * 先按 model_name 精确匹配，再按 key（model_id）兜底。
 */
export function findModelParams(
  models: Record<string, ModelConfig> | undefined,
  modelName: string,
): Record<string, unknown> | undefined {
  if (!models || !modelName) return undefined
  const byName = Object.values(models).find((m) => m.model_name === modelName)
  if (byName?.default_params) return byName.default_params
  const byKey = models[modelName]
  return byKey?.default_params
}
