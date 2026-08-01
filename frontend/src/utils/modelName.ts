/**
 * P8 模型显示名解析工具
 *
 * LLM 配置中模型以分级键（large/medium/small）存储（见 LlmSettingsPage 的
 * "模型分级 (Large/Medium/Small)" tiers 配置），输入框若直接显示分级键
 * 会展示 "large" 而非具体模型名。本工具将分级键映射为具体模型名。
 */

/** LLM 默认配置中的模型分级映射（键为分级名，值为具体模型名） */
export type ModelTiers = Record<string, string>

/**
 * 将模型分级键解析为具体模型名
 *
 * 规则：
 * - model 命中 tiers 键（large/medium/small）→ 返回 tiers 中对应的具体模型名
 * - tiers 中键值为空 → 回退返回原值（不显示空串）
 * - model 本身是具体模型名（不在 tiers 键中）→ 原样返回
 * - model 为空 → 返回空串
 *
 * @param model 当前 agent 配置的 model 字段
 * @param tiers 分级映射（来自 LLM 配置 defaults.tiers），可为 undefined
 * @returns 显示用的具体模型名
 */
export function resolveModelDisplayName(
  model: string | undefined,
  tiers: ModelTiers | undefined,
): string {
  if (!model) return ''
  if (!tiers) return model
  const resolved = tiers[model]
  return resolved && resolved.trim() ? resolved : model
}
