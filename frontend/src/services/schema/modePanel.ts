/**
 * 模式 → 面板导航目标聚合（模式体系落地设计 §5.0 观测链 / §5.3 会话态规则）
 *
 * 管道视图模式徽标的取数面。**registry 聚合**（二选一裁定，不调 mode.describe
 * 服务）：模式插件在 contributes.pages 声明 workspace/tab 面板页并携带 `mode`
 * 扩展字段（= 面板归属的模式键）——按 mode 值命中面板页声明。插件禁用 →
 * 声明同源消失 → 徽标不渲染（§5.3「查不到映射=不渲染」）；state 无 mode 键 →
 * 消费方零渲染。
 *
 * 徽标显示名/图标与选择器选项同源：模式插件的 select-option 追加声明
 * （target=task_mode，选项清单单源在各模式插件声明）。
 */

import { contributionRegistry, type PageDeclaration } from './ContributionRegistry'

/** task_mode 选择器的 select-option 追加声明形态（props） */
interface SelectOptionProps {
  target?: unknown
  value?: unknown
  label?: unknown
  icon?: unknown
}

function taskModeOptionOf(mode: string): SelectOptionProps | undefined {
  const decl = contributionRegistry
    .getAllWidgets()
    .find(
      (w) =>
        w.type === 'select-option' &&
        (w.props as SelectOptionProps | undefined)?.target === 'task_mode' &&
        (w.props as SelectOptionProps | undefined)?.value === mode,
    )
  return decl?.props as SelectOptionProps | undefined
}

/**
 * 聚合 mode 值 → 模式面板页声明。
 *
 * 未命中任何带 mode 扩展字段的 workspace/tab 声明时返回 undefined
 * （调用方不渲染徽标）。
 */
export function getModePanelTarget(mode: string): PageDeclaration | undefined {
  if (!mode) return undefined
  return contributionRegistry
    .getPagesBySpace('workspace')
    .find((p) => p.slot === 'tab' && p.mode === mode)
}

/** 模式徽标展示文案：select-option 声明 label 优先（模式显示名），回退 mode 值 */
export function getModePanelLabel(mode: string): string {
  const option = taskModeOptionOf(mode)
  return typeof option?.label === 'string' ? option.label : mode
}

/** 模式徽标图标：select-option 声明 icon（缺省 undefined） */
export function getModePanelIcon(mode: string): string | undefined {
  const option = taskModeOptionOf(mode)
  return typeof option?.icon === 'string' ? option.icon : undefined
}
