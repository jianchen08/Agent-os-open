/**
 * 思考模式类型定义
 */

export type ThinkingModeType = 'parameter_switch' | 'model_switch'

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
