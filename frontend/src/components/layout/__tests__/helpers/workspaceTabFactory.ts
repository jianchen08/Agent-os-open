/** WorkspacePanel 系测试共用的 tab 工厂（WorkspacePanel/GapsCoverage 同源） */
import type { WorkspaceTab } from '@/types/layout'

export function makeTab(overrides: Partial<WorkspaceTab> = {}): WorkspaceTab {
  return {
    id: 'tab-1',
    title: '标签1',
    isActive: true,
    isPinned: false,
    ...overrides,
  } as WorkspaceTab
}
