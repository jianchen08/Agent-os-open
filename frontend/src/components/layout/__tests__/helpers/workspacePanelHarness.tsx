/**
 * WorkspacePanel 测试家族共享的 store 驱动渲染器（closeConfirm/GapsCoverage 同源）。
 * 曾在两个测试文件逐字复制（jscpd 克隆门禁重复源）。
 */
import { WorkspacePanel } from '@/components/layout/WorkspacePanel'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 与 FiveSpaceLayout 相同的数据流：tabs/visited 取自 layoutModeStore */
export function StoreDrivenPanel(extra: Record<string, unknown> = {}) {
  const tabs = useLayoutModeStore((s) => s.workspaceTabs)
  const visitedTabIds = useLayoutModeStore((s) => s.visitedTabIds)
  return (
    <WorkspacePanel
      tabs={tabs}
      visitedTabIds={visitedTabIds}
      onTabChange={() => {}}
      onTabClose={() => {}}
      renderTabContent={(tab) => <div>内容-{tab.id}</div>}
      {...extra}
    />
  )
}
