// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * workspacePanelOpener 导航分支补充测试
 *
 * 覆盖既有测试未触达的分支：
 * 1. 打开已存在面板 → 仅 setActiveTab（不重复 add）
 * 2. 面板打开时工作区折叠 → 自动展开
 * 3. openWorkspacePanelByPath：TOP_NAV_PANELS 精确命中 / 前缀命中（/settings/xxx）/ 未命中 false
 * 4. ensureDefaultTaskPanel：已有任务面板跳过 / 空列表激活添加 / 非空列表追加不激活
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { mockLayoutStore, mockUIStore } = vi.hoisted(() => ({
  mockLayoutStore: {
    workspaceTabs: [],
    setActiveTab: vi.fn(),
    addWorkspaceTab: vi.fn(),
  },
  mockUIStore: {
    workspaceCollapsed: false,
    setWorkspaceCollapsed: vi.fn(),
  },
}))

vi.mock('@/stores/layoutModeStore', () => ({
  useLayoutModeStore: {
    getState: () => mockLayoutStore,
    setState: (fn: any) => {
      const next = typeof fn === 'function' ? fn(mockLayoutStore) : fn
      Object.assign(mockLayoutStore, next)
    },
  },
}))

vi.mock('@/stores/uiStore', () => ({
  useUIStore: { getState: () => mockUIStore },
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getPages: () => [],
  },
}))

import {
  TOP_NAV_PANELS,
  openWorkspacePanel,
  openWorkspacePanelByPath,
  ensureDefaultTaskPanel,
} from '@/services/workspacePanelOpener'

describe('openWorkspacePanel', () => {
  beforeEach(() => {
    mockLayoutStore.workspaceTabs = []
    mockLayoutStore.setActiveTab.mockClear()
    mockLayoutStore.addWorkspaceTab.mockClear()
    mockUIStore.workspaceCollapsed = false
    mockUIStore.setWorkspaceCollapsed.mockClear()
  })

  it('不存在 → 构造 tab 并 addWorkspaceTab（isActive=true，isPinned 缺省 false）', () => {
    openWorkspacePanel(TOP_NAV_PANELS['/tasks'])
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'ws-panel-tasks',
        isActive: true,
        isPinned: false,
        moduleId: '__panel_tasks__',
      }),
    )
    expect(mockLayoutStore.setActiveTab).not.toHaveBeenCalled()
  })

  it('已存在 → 只 setActiveTab，不重复添加', () => {
    mockLayoutStore.workspaceTabs = [{ id: 'ws-panel-settings' }]
    openWorkspacePanel(TOP_NAV_PANELS['/settings'])
    expect(mockLayoutStore.setActiveTab).toHaveBeenCalledWith('ws-panel-settings')
    expect(mockLayoutStore.addWorkspaceTab).not.toHaveBeenCalled()
  })

  it('工作区已折叠 → 打开面板时自动展开', () => {
    mockUIStore.workspaceCollapsed = true
    openWorkspacePanel(TOP_NAV_PANELS['/tasks'])
    expect(mockUIStore.setWorkspaceCollapsed).toHaveBeenCalledWith(false)
  })

  it('工作区未折叠 → 不调用 setWorkspaceCollapsed', () => {
    openWorkspacePanel(TOP_NAV_PANELS['/tasks'])
    expect(mockUIStore.setWorkspaceCollapsed).not.toHaveBeenCalled()
  })
})

describe('openWorkspacePanelByPath', () => {
  beforeEach(() => {
    mockLayoutStore.workspaceTabs = []
    mockLayoutStore.addWorkspaceTab.mockClear()
  })

  it('TOP_NAV_PANELS 精确命中 → 打开并返回 true', () => {
    expect(openWorkspacePanelByPath('/tasks')).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ws-panel-tasks' }),
    )
  })

  it('前缀命中（/settings/plugins 子路径）→ 打开最长匹配前缀面板', () => {
    expect(openWorkspacePanelByPath('/settings/plugins/llm')).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ws-panel-plugins' }),
    )
  })

  it('无匹配路径 → 返回 false 且不打开任何面板', () => {
    expect(openWorkspacePanelByPath('/no-such-route')).toBe(false)
    expect(mockLayoutStore.addWorkspaceTab).not.toHaveBeenCalled()
  })
})

describe('ensureDefaultTaskPanel', () => {
  beforeEach(() => {
    mockLayoutStore.workspaceTabs = []
    mockLayoutStore.addWorkspaceTab.mockClear()
  })

  it('已有任务面板 → 跳过（不重复添加）', () => {
    mockLayoutStore.workspaceTabs = [{ id: 'ws-panel-tasks' }]
    ensureDefaultTaskPanel()
    expect(mockLayoutStore.addWorkspaceTab).not.toHaveBeenCalled()
  })

  it('无任何面板 → 添加任务面板且 isActive=true', () => {
    ensureDefaultTaskPanel()
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ws-panel-tasks', isActive: true }),
    )
  })

  it('已有其他面板 → 追加任务面板且 isActive=false（走 setState 直接追加）', () => {
    mockLayoutStore.workspaceTabs = [{ id: 'other-tab', isActive: true }]
    ensureDefaultTaskPanel()
    expect(mockLayoutStore.addWorkspaceTab).not.toHaveBeenCalled()
    expect(mockLayoutStore.workspaceTabs).toHaveLength(2)
    expect(mockLayoutStore.workspaceTabs[1]).toEqual(
      expect.objectContaining({ id: 'ws-panel-tasks', isActive: false }),
    )
  })
})
