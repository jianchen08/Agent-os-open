/** 主 Tab 显示名称解析 —— 纯函数，从 ChatContainer 抽出以便单测。 */
import type { Agent } from '@/types/models'

/**
 * 主 Tab 的显示名称：用 agentId 从 agents 列表实时解析真实名称。
 *
 * 匹配优先级：a.id === agentId || a.configId === agentId（与后端 config_id 对齐）。
 *
 * 回退策略（修复 BUG-20260713）：
 * - 匹配到 → 真实 name（如 "灵汐"）
 * - 匹配不到但 agentId 非空 → 显示 agentId 原值（保留可追溯信息，避免无声掩盖）
 * - agentId 也为空 → "主Agent"
 *
 * 旧实现匹配不到就回退 "主Agent"，在 agents 列表加载竞态（fetchAgents 与
 * fetchSessions 并行）或后端返回空时，主 Tab 无声显示 "主Agent"，看起来像
 * "默认 agent"。改为回退真实 agentId 后，即使列表未就绪也能显示真实身份。
 */
export function resolveMainTabName(agentId: string | undefined, agents: Agent[]): string {
  if (!agentId) return '主Agent'
  const matched = agents.find((a) => a.id === agentId || a.configId === agentId)
  return matched?.name || agentId
}
