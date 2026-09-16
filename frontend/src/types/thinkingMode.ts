/**
 * 思考模式类型定义
 */

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
