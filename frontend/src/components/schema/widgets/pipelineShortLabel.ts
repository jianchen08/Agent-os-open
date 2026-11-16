/**
 * 短名解析（末段）——任务管理面板条目行与模式徽标共用。
 *
 * 显示名形如 `mode_coding/programming_orchestrator_agent_v2`：行内只显末段
 * 短名，`mode_xxx/` 前缀与全名进 title 悬停提示——全名过长会把任务标题挤出
 * 视野。模式 ID 同形（`mode_coding/code_writer` → `code_writer`）。
 *
 * 纯函数（无副作用），供渲染面与单测共用。
 */

/**
 * 取 id 全名的 stem 短名（末段）。
 *
 * - `mode_coding/programming_orchestrator_agent_v2` → `programming_orchestrator_agent_v2`
 * - `mode_coding/code_writer` → `code_writer`（模式 ID 同形）
 * - `general_agent`（无 '/'）→ `general_agent`（原样）
 * - `a/b/c` → `c`（取末段）
 * - 空串 / 纯空白 → 空串
 * - 结尾带 '/'（如 `mode_coding/`）→ 原样（无有效 stem，不制造空徽标）
 */
export function stemLabel(fullName: string): string {
  const trimmed = fullName.trim()
  if (!trimmed) return ''
  const slash = trimmed.lastIndexOf('/')
  if (slash < 0 || slash === trimmed.length - 1) return trimmed
  return trimmed.slice(slash + 1)
}

/**
 * 执行身份 agent.id → 行内显示名（用户裁定 2026-11-06：任务管理面板行首
 * 身份位直接显 agent 名——主管道显「灵汐」，不显代号 ID）。
 *
 * - agents 注册表（id/config_id 双键匹配）命中 → 配置显示名（`agentos` →
 *   `灵汐`；模式键 `mode_X/<stem>` 按完整键命中模式包 agent 名）
 * - 未命中 → stem 短名回退；空值 → undefined（行内不渲染空徽标）
 *
 * 纯函数（无副作用），供渲染面与单测共用。
 */
export function resolveAgentLabel(
  agentId: string | undefined,
  agents: ReadonlyArray<{ id?: string; configId?: string; name?: string }>,
): string | undefined {
  const trimmed = agentId?.trim()
  if (!trimmed) return undefined
  const hit = agents.find((a) => a.id === trimmed || a.configId === trimmed)
  return hit?.name?.trim() || stemLabel(trimmed)
}
