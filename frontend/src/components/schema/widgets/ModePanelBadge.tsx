/**
 * ModePanelBadge · 管道视图模式徽标插槽（模式体系落地设计 §5.0 观测链）
 *
 * 管道可视化/任务树条目行上的模式徽标：读管道 state 的 mode 键 → 经
 * modePanel 聚合（registry 取数）解析模式面板页 → 点击 openPluginPage 打开
 * 面板（观测链「徽标 → 面板」双向路径的前向导航）。无 mode 键或查不到映射
 * （插件禁用/无关会话 §5.3）一律不渲染——零 mode 零渲染。
 */

import { getModePanelIcon, getModePanelLabel, getModePanelTarget } from '@/services/schema/modePanel'
import { openPluginPage } from '@/services/workspacePanelOpener'

export function ModePanelBadge({ mode }: { mode: string }) {
  const target = getModePanelTarget(mode)
  if (!target) return null
  const label = getModePanelLabel(mode)
  const icon = getModePanelIcon(mode)
  return (
    <button
      type="button"
      data-testid={`mode-badge-${mode}`}
      title={`模式：${label}（点击打开面板）`}
      aria-label={`模式 ${label}，打开模式面板`}
      className="bg-primary/10 text-primary/80 hover:bg-primary/20 hidden shrink-0 cursor-pointer items-center gap-0.5 rounded px-1 py-0 text-[10px] font-medium sm:inline-flex"
      onClick={(e) => {
        // 不触发行点击（行点击 = 打开对话标签，语义不同）
        e.stopPropagation()
        openPluginPage(target)
      }}
    >
      {icon && (
        <span aria-hidden="true" className="text-[10px] leading-none">
          {icon}
        </span>
      )}
      {label}
    </button>
  )
}

export default ModePanelBadge
