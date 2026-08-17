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
 * 强度 → 思考参数映射（随消息传给后端 llm_core 路由；模型级配置
 * thinking_strength_params 优先，本表为兜底基线）。
 * 决策：只覆盖思考相关参数（reasoning_effort）；
 * temperature/max_tokens 等采样参数不随强度变化（始终用模型 default_params）。
 * off → 不覆盖（保持 llm.yaml default_params 现状）。
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

export interface ThinkingModeConfig {
  /** 模型名称 */
  modelName: string
  /** 显示名称 */
  displayName: string
  /** 思考模式类型 */
  thinkingType: ThinkingModeType
  /** 基础模型 */
  baseModel: string
  /** 思考模型 */
  thinkingModel: string
  /** 是否为同一模型 */
  isSameModel: boolean
  /** 是否支持推理强度 */
  supportsReasoningEffort: boolean
  /** 描述 */
  description: string
  /** 切换描述 */
  switchDescription: string
}

export interface ThinkingModeSwitchOptions {
  /** 当前模型 */
  currentModel: string
  /** 是否启用思考模式 */
  enableThinking: boolean
  /** 任务类型（可选） */
  taskType?: string
  /** 复杂度（可选） */
  complexity?: string
}

export interface ThinkingModeRecommendationItem {
  /** 模型名称 */
  modelName: string
  /** 显示名称 */
  displayName: string
  /** 思考模式类型 */
  thinkingType: ThinkingModeType
  /** 适合度评分 */
  suitabilityScore: number
  /** 最优参数 */
  optimalParams: Record<string, any>
  /** 最适合的场景 */
  bestFor: string[]
  /** 使用建议 */
  tips: string[]
  /** 成本估算 */
  costEstimate: string
}
