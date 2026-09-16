/**
 * 任务模式键契约与发送链并入（模式体系落地设计 §4.2 数据链）
 *
 * - 契约常量：任务上下文字段 `mode`，值 coding|writing|roleplay|research
 *   （「自动」= 不带键，模式归属由后端自然语言分类路径裁决）。
 * - 选项面（UI）不在本模块：任务模式选择器由 task_form 插件的 ui_schema
 *   form select 声明（id=task_mode，选项含图标写死于声明）+ 宿主受控桥渲染，
 *   与权限模式/思考强度选择器同构；模式面板徽标配对见 modePanel.ts。
 * - withTaskMode：发送时把所选模式并入消息级 execution_context（会话执行选项
 *   其余键保持原样），内核 1a2 合并点透传至任务上下文。
 */

/** 模式键契约值（冻结，不得更改） */
export type TaskMode = 'coding' | 'writing' | 'roleplay' | 'research'

/** 模式键契约值集合 */
export const TASK_MODES: readonly TaskMode[] = ['coding', 'writing', 'roleplay', 'research']

/**
 * 模式键并入消息级 execution_context
 *
 * 「自动」（mode 缺席）不带键——原 execution_context 原样返回（含 undefined）；
 * 显式选择时浅合并出新的 context（不改动会话执行选项快照对象）。
 */
export function withTaskMode(
  executionContext: Record<string, unknown> | undefined,
  mode: TaskMode | undefined,
): Record<string, unknown> | undefined {
  if (!mode) return executionContext
  return { ...executionContext, mode }
}
