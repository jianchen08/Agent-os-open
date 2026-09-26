/**
 * 顶栏/侧栏打开工作区页签
 *
 * VS Code 模型：导航入口不是常驻标签，点击后在 Workspace 打开/激活可关闭页签。
 */

import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useUIStore } from '@/stores/uiStore'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import type { WorkspaceTab } from '@/types/layout'

export interface WorkspacePanelSpec {
  /** 稳定 id，重复打开时激活已有 tab */
  id: string
  title: string
  /** 传给 widgetRegistry 的 component 名 */
  component: string
  icon?: string
  /** 归属的插件页面声明 id（detachable 弹出等页面级能力的解析键） */
  pageId?: string
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
  // 任务管理归前端自有（用户裁定 2026-09-21）：声明在本表，不依赖插件
  // contributes.pages——插件只提供后端能力（任务 API/服务/工具面）。页签 id
  // 与 layoutModeStore 默认页签同源（ws-panel-tasks），故重复打开幂等激活。
  '/tasks': {
    id: 'ws-panel-tasks',
    title: '任务管理',
    component: 'pipeline_manager',
    icon: 'folder',
    moduleId: '__panel_tasks__',
  },
  // 监控/成本/智能体均已声明化（各插件 contributes.pages 按 path
  // 声明，解析顺序 1），禁用插件即移除入口，此处不再硬编码。
  // 「插件管理」双入口已收敛：设置中枢内核组 kernel-plugins 渲染同一
  // PluginsSettingsPage，独立 '/settings/plugins' 面板条目已撤。
}

/** 「新建标签页」目标：浏览器式新标签页=导航页（WorkspaceNavPage 卡片网格，
 * 与空标签态兜底同一内容源）。widget 名与 registerWidgets 注册一致。 */
export const WORKSPACE_NAV_TAB: WorkspacePanelSpec = {
  id: 'ws-panel-workspace-nav',
  title: '导航',
  component: 'workspace_nav_page',
  icon: '🧭',
  moduleId: '__panel_workspace_nav__',
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
      pageId: spec.pageId,
      dataSource: spec.dataSource,
      props: spec.props,
      isActive: true,
      isPinned: spec.isPinned ?? false,
    }
    store.addWorkspaceTab(tab)
  }

  // 打开面板时展开工作区（若用户已折叠）。
  const ui = useUIStore.getState()
  if (ui.workspaceCollapsed) {
    ui.setWorkspaceCollapsed(false)
  }
}

/**
 * 插件页面声明 → 工作区面板 spec
 *（openWorkspacePanelByPath 的 path 解析分支与 openPluginPage 共用同一映射）
 */
function pluginPageSpec(page: PageDeclaration): WorkspacePanelSpec {
  return {
    id: `ws-plugin-${page.id}`,
    title: page.title || page.id,
    component: page.widget || page.id,
    icon: page.icon,
    pageId: page.id,
    dataSource: page.datasourceUri,
    props: page.props,
    moduleId: page.pluginId ? `__plugin_${page.pluginId}__` : `__contrib_${page.id}__`,
  }
}

/**
 * config_files 配置声明页 → 设置中枢深链页签
 *
 * plugin_configs 归一化的配置页（legacyFrom='settingsPanels'，无 path/widget）
 * 唯一渲染落点是设置中枢（「设置」唯一 UI）：按 StepNode.openPluginConfig 同款
 * 规格，每配置文件一个稳定页签（重复打开幂等激活），props.initialActive 深链
 * 直达目标配置页。页 id 为 `{pluginId}:{fileId}`（ContributionRegistry 归一化契约）。
 */
function pluginConfigSpec(page: PageDeclaration): WorkspacePanelSpec | null {
  if (page.legacyFrom !== 'settingsPanels' || !page.pluginId) return null
  const fileId = page.id.startsWith(`${page.pluginId}:`)
    ? page.id.slice(page.pluginId.length + 1)
    : page.id
  return {
    id: `ws-plugin-config-${page.pluginId}-${fileId}`,
    title: page.title || fileId,
    component: 'settings_hub',
    icon: 'settings',
    moduleId: '__panel_settings__',
    props: { initialActive: `plugin:${page.pluginId}:${fileId}` },
  }
}

/**
 * 打开插件贡献页面（插件页面导航面板等导航消费方用）
 *
 * 解析顺序：
 * 1. 声明 path → 走既有 openWorkspacePanelByPath（path 声明是页面直达语义的单入口）
 * 2. 声明 widget → 按声明直接开工作区页签（与 path 解析的插件页分支同映射）
 * 3. config_files 配置页（plugin_configs 归一化产物，无 path/widget）→ 设置中枢深链页签
 * 4. 其余无 path 也无 widget → 显式报错，绝不静默（与 openWorkspacePanelByPath 失败口径一致）
 */
export function openPluginPage(page: PageDeclaration): boolean {
  if (page.path) return openWorkspacePanelByPath(page.path)
  if (page.widget) {
    openWorkspacePanel(pluginPageSpec(page))
    return true
  }
  const configSpec = pluginConfigSpec(page)
  if (configSpec) {
    openWorkspacePanel(configSpec)
    return true
  }
  useNotificationStore.getState().addNotification({
    title: '页面无法打开',
    message: `页面 ${page.title || page.id} 未声明 path 或 widget，没有可直达的渲染目标（配置类页面请从设置中枢进入）`,
    priority: 'high',
    category: 'error',
    isBlocking: false,
    autoDismissMs: 8000,
    sourceLabel: '插件页面',
  })
  return false
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
 *
 * 解析失败契约：显式报错（error 通知含目标 path），绝不静默——调用方若再
 * navigate 兜底会落到 '*' 通配 → 回首页，掩盖声明缺失（P0-1）。
 */
export function openWorkspacePanelByPath(path: string): boolean {
  // 1) 插件页面按 path 直达（component 取 page.widget，未声明 widget 时用 page.id 兜底）
  const pluginPage = contributionRegistry.getPages().find((p) => p.path === path)
  if (pluginPage) {
    openWorkspacePanel(pluginPageSpec(pluginPage))
    return true
  }
  // 2) 精确匹配
  const exact = TOP_NAV_PANELS[path]
  if (exact) {
    openWorkspacePanel(exact)
    return true
  }
  // 3) 前缀匹配（如 /settings/xxx）。比较器仅在 ≥2 个前缀键同时命中时被调用；
  // 现有 TOP_NAV_PANELS 前缀键唯一（'/settings'），单元素 sort 不调比较器——
  // 键扩容出现嵌套前缀时本行自然可达。
  const prefix = Object.keys(TOP_NAV_PANELS)
    .filter((k) => k.startsWith('/') && path.startsWith(k))
    // v8 ignore next
    .sort((a, b) => b.length - a.length)[0]
  if (prefix) {
    openWorkspacePanel(TOP_NAV_PANELS[prefix])
    return true
  }
  useNotificationStore.getState().addNotification({
    title: '面板打开失败',
    message: `路径 ${path} 没有对应的插件页面声明或内置面板，请检查提供该面板的插件是否已启用`,
    priority: 'high',
    category: 'error',
    isBlocking: false,
    autoDismissMs: 8000,
    sourceLabel: '工作区',
  })
  return false
}
