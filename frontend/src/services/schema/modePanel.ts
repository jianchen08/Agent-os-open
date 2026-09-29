/**
 * 模式 → 面板导航目标聚合（模式体系落地设计 §5.0 观测链 / §5.3 会话态规则）
 *
 * 管道视图模式徽标的取数面。**registry 聚合**（二选一裁定，不调 mode.describe
 * 服务）：模式插件在 contributes.pages 声明 workspace/tab 面板页并携带 `mode`
 * 扩展字段（= 面板归属的模式键）——按 mode 值命中面板页声明。插件禁用 →
 * 声明同源消失 → 徽标不渲染（§5.3「查不到映射=不渲染」）；state 无 mode 键 →
 * 消费方零渲染。
 *
 * 徽标显示名/图标与选择器选项同源：modes registry 声明派生（mode.yaml
 * name/icon，经 useModesRegistry 单源取数——选择器选项同函数族，
 * taskModeOptionsFromModes）。
 */

import { contributionRegistry, type PageDeclaration } from './ContributionRegistry'
import type { ModeDeclaration, ModesRegistryResponse } from '@/services/api/modes'

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

/** registry 内按 mode 键取声明（缺席 = undefined） */
export function modeDeclarationOf(
  mode: string,
  registry: ModesRegistryResponse | undefined,
): ModeDeclaration | undefined {
  if (!mode) return undefined
  return registry?.modes.find((m) => m.mode === mode)
}

/** 模式徽标展示文案：registry 声明 name 优先，回退 mode 值 */
export function getModePanelLabel(mode: string, registry: ModesRegistryResponse | undefined): string {
  return modeDeclarationOf(mode, registry)?.name ?? mode
}

/** 模式徽标图标：registry 声明 icon（缺省 undefined） */
export function getModePanelIcon(
  mode: string,
  registry: ModesRegistryResponse | undefined,
): string | undefined {
  const icon = modeDeclarationOf(mode, registry)?.icon
  return typeof icon === 'string' && icon ? icon : undefined
}
