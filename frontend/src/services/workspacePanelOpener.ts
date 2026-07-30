/**
 * 顶栏/侧栏打开工作区页签
 *
 * VS Code 模型：导航入口不是常驻标签，点击后在 Workspace 打开/激活可关闭页签。
 */

import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import type { WorkspaceTab } from '@/types/layout'

export interface WorkspacePanelSpec {
  /** 稳定 id，重复打开时激活已有 tab */
  id: string
  title: string
  /** 传给 widgetRegistry 的 component 名 */
  component: string
  icon?: string
  moduleId?: string
  dataSource?: string
  /** 是否固定（默认 false，顶栏打开的可关） */
  isPinned?: boolean
}

/** 内置顶栏入口对应的工作区面板 */
export const TOP_NAV_PANELS: Record<string, WorkspacePanelSpec> = {
  '/settings': {
    id: 'ws-panel-settings',
    title: '设置',
    component: 'settings_hub',
    icon: 'settings',
    moduleId: '__panel_settings__',
  },
  '/settings/plugins': {
    id: 'ws-panel-plugins',
    title: '插件管理',
    component: 'plugins_panel',
    icon: 'plugin',
    moduleId: '__panel_plugins__',
  },
  '/monitoring': {
    id: 'ws-panel-monitoring',
    title: '监控',
    component: 'monitoring_panel',
    icon: 'activity',
    moduleId: '__panel_monitoring__',
  },
  '/tools': {
    id: 'ws-panel-tools',
    title: '工具',
    component: 'tools_panel',
    icon: 'tool',
    moduleId: '__panel_tools__',
  },
  '/agents': {
    id: 'ws-panel-agents',
    title: '智能体',
    component: 'agents_panel',
    icon: 'person',
    moduleId: '__panel_agents__',
  },
  '/memory': {
    id: 'ws-panel-memory',
    title: '记忆',
    component: 'memory_panel',
    icon: 'brain',
    moduleId: '__panel_memory__',
  },
  workspace: {
    id: 'ws-panel-workspace',
    title: '工作区',
    component: 'workspace_explorer',
    icon: 'folder',
    moduleId: '__panel_workspace__',
    isPinned: true,
  },
}

/**
 * 打开或激活一个工作区页签
 */
export function openWorkspacePanel(spec: WorkspacePanelSpec): void {
  const store = useLayoutModeStore.getState()
  const existing = store.workspaceTabs.find((t) => t.id === spec.id)
  if (existing) {
    store.setActiveTab(spec.id)
  } else {
    const tab: WorkspaceTab = {
      id: spec.id,
      title: spec.title,
      icon: spec.icon,
      moduleId: spec.moduleId || `__panel__${spec.id}`,
      component: spec.component,
      dataSource: spec.dataSource,
      isActive: true,
      isPinned: spec.isPinned ?? false,
    }
    store.addWorkspaceTab(tab)
  }

  // 打开面板时展开工作区（若用户已折叠）。
  // 例外：工作区处于「最大化」时不展开——最大化模式下工作区本就独占主区域，
  // 新 Tab 应在工作区内打开，而非退出最大化。
  const ui = useUIStore.getState()
  if (!ui.workspaceMaximized && ui.workspaceCollapsed) {
    ui.setWorkspaceCollapsed(false)
  }
}

/**
 * 按路由 path 打开对应工作区面板（顶栏导航用）
 * 无映射则返回 false，调用方可 fallback 到路由跳转
 */
export function openWorkspacePanelByPath(path: string): boolean {
  // 精确匹配
  const exact = TOP_NAV_PANELS[path]
  if (exact) {
    openWorkspacePanel(exact)
    return true
  }
  // 前缀匹配（如 /settings/xxx）
  const prefix = Object.keys(TOP_NAV_PANELS)
    .filter((k) => k.startsWith('/') && path.startsWith(k))
    .sort((a, b) => b.length - a.length)[0]
  if (prefix) {
    openWorkspacePanel(TOP_NAV_PANELS[prefix])
    return true
  }
  return false
}

/**
 * 确保工作区默认页签存在（可关闭的“打开即可”模型 + 1 个钉住工作��）
 */
export function ensureDefaultWorkspacePanels(): void {
  const store = useLayoutModeStore.getState()
  const hasWorkspace = store.workspaceTabs.some((t) => t.id === 'ws-panel-workspace')
  if (!hasWorkspace) {
    // 先 push 不激活占用，再按是否空决定激活
    const shouldActivate = store.workspaceTabs.length === 0
    const tab: WorkspaceTab = {
      id: TOP_NAV_PANELS.workspace.id,
      title: TOP_NAV_PANELS.workspace.title,
      icon: TOP_NAV_PANELS.workspace.icon,
      moduleId: TOP_NAV_PANELS.workspace.moduleId!,
      component: TOP_NAV_PANELS.workspace.component,
      isActive: shouldActivate,
      isPinned: true,
    }
    if (shouldActivate) {
      store.addWorkspaceTab(tab)
    } else {
      useLayoutModeStore.setState((s) => ({
        workspaceTabs: [...s.workspaceTabs, { ...tab, isActive: false }],
      }))
    }
  }
}
