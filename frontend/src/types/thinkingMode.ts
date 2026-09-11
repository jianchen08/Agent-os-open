/**
 * 思考模式类型定义
 */

export type ThinkingModeType = 'parameter_switch' | 'model_switch'

/** 思考强度档位：关闭 / 低 / 中 / 高 */
export type ThinkingStrength = 'off' | 'low' | 'medium' | 'high'

/** 默认思考强度（中） */
export const DEFAULT_THINKING_STRENGTH: ThinkingStrength = 'medium'

/** 强度 → 是否启用思考（off 关闭，其余启用） */
export const STRENGTH_TO_ENABLE: Record<ThinkingStrength, boolean> = {
  off: false,
  low: true,
  medium: true,
  high: true,
}

/**
 * 强度 → 思考参数映射（历史表，当前无消费方）。
 *
 * 实际路由在后端：档位随 user_input 透传到 llm_core，由 llm.yaml 的
 * providers/models 级 thinking_strength_params 解析（厂商事实唯一落点，
 * 见 ADR 2026-09-03）。本表保留仅供类型引用，值不代表线上生效结果。
 */
export const STRENGTH_TO_PARAMS: Record<
  ThinkingStrength,
  { reasoning_effort?: string } | null
> = {
  off: null,
  low: { reasoning_effort: 'low' },
  medium: { reasoning_effort: 'medium' },
  high: { reasoning_effort: 'high' },
}

export interface ThinkingModeState {
  /** 是否启用思考模式 */
  enabled: boolean
  /** 当前模型名称 */
  currentModel: string
  /** 思考模式类型 */
  thinkingType?: ThinkingModeType
  /** 是否正在切换 */
  switching: boolean
  /** 错误信息 */
  error?: string
}

