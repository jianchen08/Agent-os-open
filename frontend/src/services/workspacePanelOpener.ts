/**
 * 顶栏/侧栏打开工作区页签
 *
 * VS Code 模型：导航入口不是常驻标签，点击后在 Workspace 打开/激活可关闭页签。
 */

import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useUIStore } from '@/stores/uiStore'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
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
  /** 声明透传的静态 widget props */
  props?: Record<string, unknown>
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
  // 监控/记忆面板已声明化（widget 化 T11）：monitoring/hindsight_memory 插件
  // contributes.pages 声明（openWorkspacePanelByPath 解析顺序 1），禁用插件
  // 即移除入口；此处不再硬编码。
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
  '/tasks': {
    id: 'ws-panel-tasks',
    title: '任务管理',
    component: 'pipeline_manager',
    icon: 'folder',
    moduleId: '__panel_tasks__',
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
      props: spec.props,
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
 *
 * 解析顺序：
 * 1. 插件贡献页面（contributes.pages / 旧 viewsContainers 归一化）按 path 精确匹配
 *    —— 插件页面可经路由/路径直达，在 Workspace 打开可关闭页签
 * 2. 静态内置顶栏面板（TOP_NAV_PANELS）精确匹配
 * 3. 静态内置面板前缀匹配（如 /settings/xxx）
 */
export function openWorkspacePanelByPath(path: string): boolean {
  // 1) 插件页面按 path 直达（component 取 page.widget，未声明 widget 时用 page.id 兜底）
  const pluginPage = contributionRegistry.getPages().find((p) => p.path === path)
  if (pluginPage) {
    openWorkspacePanel({
      id: `ws-plugin-${pluginPage.id}`,
      title: pluginPage.title || pluginPage.id,
      component: pluginPage.widget || pluginPage.id,
      icon: pluginPage.icon,
      dataSource: pluginPage.datasourceUri,
      props: pluginPage.props,
      moduleId: pluginPage.pluginId ? `__plugin_${pluginPage.pluginId}__` : `__contrib_${pluginPage.id}__`,
    })
    return true
  }
  // 2) 精确匹配
  const exact = TOP_NAV_PANELS[path]
  if (exact) {
    openWorkspacePanel(exact)
    return true
  }
  // 3) 前缀匹配（如 /settings/xxx）
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
 * 确保默认「任务管理」页签存在（右侧面板打开即直接展示任务管理，非钉住可关闭）。
 * 面板是内容承载区：默认展示任务管理，用户可关闭后经入口重新打开。
 */
export function ensureDefaultTaskPanel(): void {
  const store = useLayoutModeStore.getState()
  const hasTaskPanel = store.workspaceTabs.some((t) => t.id === 'ws-panel-tasks')
  if (hasTaskPanel) return
  const shouldActivate = store.workspaceTabs.length === 0
  const spec = TOP_NAV_PANELS['/tasks']
  const tab: WorkspaceTab = {
    id: spec.id,
    title: spec.title,
    icon: spec.icon,
    moduleId: spec.moduleId || `__panel__${spec.id}`,
    component: spec.component,
    isActive: shouldActivate,
    isPinned: false,
  }
  if (shouldActivate) {
    store.addWorkspaceTab(tab)
  } else {
    useLayoutModeStore.setState((s) => ({
      workspaceTabs: [...s.workspaceTabs, { ...tab, isActive: false }],
    }))
  }
}
