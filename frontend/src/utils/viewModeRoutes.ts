/**
 * 审批视图模式声明（widget 化 T10）：view_mode → widget 路由映射。
 *
 * 数据归属：review_service 插件。声明走 manifest
 * capabilities.tools[].ui.view_modes（与 chat_card 同通道，/api/v1/schema
 * tools[] 原样透传），GrowthLoop 装载进本注册表。
 *
 * 双路由（对齐槽位架构）：插件声明（view_mode→widget 名，widgetRegistry
 * 解析组件）优先——插件声明新 view_mode 即可路由到已注册 widget，前端
 * 路由器零改动；未声明回退内置三视图（前端默认件，ApprovalRouter 直连
 * 组件渲染，不依赖 registry 初始化时序），最终兜底 text_diff。
 */

export interface ViewModeDecl {
  viewMode: string
  widget: string
}

/** 内置默认件映射（三视图同构兜底；声明覆盖其路由） */
const DEFAULT_VIEW_MODE_WIDGETS: Record<string, string> = {
  text_diff: 'text_diff',
  image_annotation: 'image_annotation',
  media_timeline: 'media_timeline',
}

const viewModeDeclarations = new Map<string, ViewModeDecl>()

/** 从 schema.tools[].ui.view_modes 装载声明（幂等：先清空再装） */
export function loadViewModes(
  tools: Array<{ ui?: { view_modes?: unknown } }>,
): void {
  viewModeDeclarations.clear()
  for (const t of tools) {
    const decls = t.ui?.view_modes
    if (!Array.isArray(decls)) continue
    for (const raw of decls) {
      if (!raw || typeof raw !== 'object') continue
      const decl = raw as { view_mode?: unknown; widget?: unknown }
      if (typeof decl.view_mode !== 'string' || decl.view_mode === '') continue
      if (typeof decl.widget !== 'string' || decl.widget === '') continue
      viewModeDeclarations.set(decl.view_mode, { viewMode: decl.view_mode, widget: decl.widget })
    }
  }
}

/** 按 view_mode 查声明 */
export function getViewModeDecl(viewMode: string): ViewModeDecl | undefined {
  return viewModeDeclarations.get(viewMode)
}

/** 清空声明注册表（测试用） */
export function clearViewModes(): void {
  viewModeDeclarations.clear()
}

export interface ViewModeRoute {
  viewMode: string
  widget: string
  /** declared = 插件声明路由（组件经 widgetRegistry 解析）；default = 内置默认件 */
  source: 'declared' | 'default'
}

/**
 * 解析 view_mode 的 widget 路由：声明优先（可覆盖内置，如 text_diff →
 * 自定义 widget），未声明回退内置同构映射；完全未知返回 null（调用方
 * 兜底 text_diff）。
 */
export function resolveViewModeRoute(viewMode: string): ViewModeRoute | null {
  const declared = viewModeDeclarations.get(viewMode)
  if (declared) {
    return { viewMode, widget: declared.widget, source: 'declared' }
  }
  const widget = DEFAULT_VIEW_MODE_WIDGETS[viewMode]
  if (widget) return { viewMode, widget, source: 'default' }
  return null
}
