/**
 * ModePanelBadge · 管道视图模式徽标插槽（模式体系落地设计 §5.0 观测链）
 *
 * 管道可视化/任务树条目行上的模式徽标：读管道 state 的 mode 键 → 经
 * modePanel 聚合（registry 取数）解析模式面板页 → 点击 openPluginPage 打开
 * 面板（观测链「徽标 → 面板」双向路径的前向导航）。无 mode 键或查不到映射
 * （插件禁用/无关会话 §5.3）一律不渲染——零 mode 零渲染。
 */

import { useModesRegistry } from '@/services/api/modes'
import { getModePanelLabel, getModePanelTarget } from '@/services/schema/modePanel'
import { openPluginPage } from '@/services/workspacePanelOpener'
import { stemLabel } from './pipelineShortLabel'

export function ModePanelBadge({ mode }: { mode: string }) {
  const registry = useModesRegistry()
  const target = getModePanelTarget(mode)
  if (!target) return null
  const rawLabel = getModePanelLabel(mode, registry)
  const shortId = stemLabel(mode)
  // 单一表达形态（同类信息只留一种表达形态）：纯文字，不叠图标；
  // 模式 ID 只显短名（不显全路径）
  const displayName = rawLabel === mode ? shortId : rawLabel
  const tipName = rawLabel === mode ? shortId : `${rawLabel}（${shortId}）`
  return (
    <button
      type="button"
      data-testid={`mode-badge-${mode}`}
      title={`模式：${tipName}（点击打开面板）`}
      aria-label={`模式 ${displayName}，打开模式面板`}
      className="bg-primary/10 text-primary/80 hover:bg-primary/20 inline-flex shrink-0 cursor-pointer items-center rounded px-1 py-0 text-[10px] font-medium"
      onClick={(e) => {
        // 不触发行点击（行点击 = 切换详情展开，语义不同）
        e.stopPropagation()
        openPluginPage(target)
      }}
    >
      {displayName}
    </button>
  )
}

export default ModePanelBadge

/**
 * ModeTagBadges · 模式标签集渲染（模式状态键标签集化）。
 *
 * 集内逐标签渲染模式徽标（各标签独立走 modePanel 聚合解析，查不到映射的
 * 标签不渲染——与单徽标同契约）；空集零渲染。
 */
export function ModeTagBadges({ modeTags }: { modeTags?: string[] }) {
  if (!modeTags?.length) return null
  return (
    <>
      {modeTags.map((tag) => (
        <ModePanelBadge key={tag} mode={tag} />
      ))}
    </>
  )
}

