// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * workspacePanelOpener 导航分支补充测试
 *
 * 覆盖既有测试未触达的分支：
 * 1. 打开已存在面板 → 仅 setActiveTab（不重复 add）
 * 2. 面板打开时工作区折叠 → 自动展开
 * 3. openWorkspacePanelByPath：TOP_NAV_PANELS 精确命中 / 前缀命中（/settings/xxx）/ 未命中 false
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
  openPluginPage,
  openWorkspacePanel,
  openWorkspacePanelByPath,
} from '@/services/workspacePanelOpener'
import { useNotificationStore } from '@/stores/notificationStore'

describe('openWorkspacePanel', () => {
  beforeEach(() => {
    mockLayoutStore.workspaceTabs = []
    mockLayoutStore.setActiveTab.mockClear()
    mockLayoutStore.addWorkspaceTab.mockClear()
    mockUIStore.workspaceCollapsed = false
    mockUIStore.setWorkspaceCollapsed.mockClear()
  })

  it('不存在 → 构造 tab 并 addWorkspaceTab（isActive=true，isPinned 缺省 false）', () => {
    openWorkspacePanel(TOP_NAV_PANELS['/settings'])
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({
        id: 'ws-panel-settings',
        isActive: true,
        isPinned: false,
        moduleId: '__panel_settings__',
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
    openWorkspacePanel(TOP_NAV_PANELS['/settings'])
    expect(mockUIStore.setWorkspaceCollapsed).toHaveBeenCalledWith(false)
  })

  it('工作区未折叠 → 不调用 setWorkspaceCollapsed', () => {
    openWorkspacePanel(TOP_NAV_PANELS['/settings'])
    expect(mockUIStore.setWorkspaceCollapsed).not.toHaveBeenCalled()
  })
})

describe('openWorkspacePanelByPath', () => {
  beforeEach(() => {
    mockLayoutStore.workspaceTabs = []
    mockLayoutStore.addWorkspaceTab.mockClear()
  })

  it('TOP_NAV_PANELS 精确命中 → 打开并返回 true', () => {
    expect(openWorkspacePanelByPath('/settings')).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ws-panel-settings' }),
    )
  })

  it('前缀命中（/settings 子路径）→ 打开最长匹配前缀面板', () => {
    // 独立 plugins_panel 条目已撤：/settings/plugins/* 落 /settings 前缀（设置中枢）
    expect(openWorkspacePanelByPath('/settings/plugins/llm')).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledWith(
      expect.objectContaining({ id: 'ws-panel-settings' }),
    )
  })

  it('无匹配路径 → 返回 false 且不打开任何面板', () => {
    expect(openWorkspacePanelByPath('/no-such-route')).toBe(false)
    expect(mockLayoutStore.addWorkspaceTab).not.toHaveBeenCalled()
  })
})

describe('openWorkspacePanelByPath - 前缀匹配', () => {
  it('非精确路径（/settings/xxx）→ 前缀命中 /settings 面板', () => {
    mockLayoutStore.addWorkspaceTab.mockClear()
    const ok = openWorkspacePanelByPath('/settings/agents-xxx')
    expect(ok).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalledTimes(1)
  })
})

describe('openPluginPage - 插件贡献页直达', () => {
  it('无 path 也无 widget → 显式通知「页面无法打开」并返回 false', () => {
    const ok = openPluginPage({ id: 'cfg-page', title: '某配置页' })
    expect(ok).toBe(false)
    const { notifications } = useNotificationStore.getState()
    expect(notifications.some((n) => n.message.includes('未声明 path 或 widget'))).toBe(true)
  })

  it('声明 widget → 按声明开工作区页签并返回 true', () => {
    const ok = openPluginPage({ id: 'w-page', title: '构件页', widget: 'some_widget' })
    expect(ok).toBe(true)
    expect(mockLayoutStore.addWorkspaceTab).toHaveBeenCalled()
  })
})
