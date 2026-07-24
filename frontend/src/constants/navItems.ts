/**
 * 导航配置注册表
 *
 * 替代 AppHeader 中硬编码的 NAV_ITEMS，支持动态扩展。
 * 模块/插件可通过 schemaRegistry 注册导航项，AppHeader 从这里读取。
 *
 * ADR §5.7：导航收敛——顶栏导航、activity bar 图标、侧边栏视图切换，
 * 全部由 contributes.viewsContainers + contributes.views 动态生成。
 */

import { ROUTES } from '@/constants/routes'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

/** 导航项定义 */
export interface NavItem {
  /** 路由路径 */
  path: string
  /** 显示标签 */
  label: string
  /** 图标（emoji 或组件名） */
  icon?: string
  /** 排序权重（越小越靠前） */
  order?: number
  /** 是否仅管理员可见 */
  adminOnly?: boolean
  /** 来源：kernel=内核固定，contributes=插件贡献 */
  source?: 'kernel' | 'contributes'
}

/** 默认导航项（内核固定） */
const DEFAULT_NAV_ITEMS: NavItem[] = [
  { path: ROUTES.TOOLS, label: '工具', order: 10, source: 'kernel' },
  { path: ROUTES.AGENTS, label: '智能体', order: 20, source: 'kernel' },
  { path: ROUTES.MONITORING, label: '监控', order: 30, source: 'kernel' },
  { path: ROUTES.MEMORY, label: '记忆', order: 40, source: 'kernel' },
  { path: ROUTES.SETTINGS, label: '设置', order: 50, source: 'kernel' },
  { path: ROUTES.DEBUG.ROOT, label: '调试', order: 90, adminOnly: true, source: 'kernel' },
]

/** 额外注册的导航项（模块/插件动态添加） */
const dynamicNavItems: NavItem[] = []

/**
 * 注册额外导航项
 *
 * 模块/插件可通过此 API 注册自己的导航项。
 *
 * @param item - 导航项配置
 */
export function registerNavItem(item: NavItem): void {
  if (dynamicNavItems.some((i) => i.path === item.path)) return
  dynamicNavItems.push(item)
  dynamicNavItems.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
}

/**
 * 注销导航项
 */
export function unregisterNavItem(path: string): void {
  const idx = dynamicNavItems.findIndex((i) => i.path === path)
  if (idx >= 0) dynamicNavItems.splice(idx, 1)
}

/**
 * 从 ContributionRegistry 同步导航项
 *
 * 将 contributes.viewsContainers 映射为导航项。
 * 每次 schema 更新后调用，确保导航与插件状态同步。
 */
export function syncNavItemsFromContributes(): void {
  // 清除旧的 contributes 导航项
  for (let i = dynamicNavItems.length - 1; i >= 0; i--) {
    if (dynamicNavItems[i].source === 'contributes') {
      dynamicNavItems.splice(i, 1)
    }
  }

  // 从 ContributionRegistry 注册新的导航项
  const containers = contributionRegistry.getViewsContainers()
  for (const container of containers) {
    if (container.path) {
      registerNavItem({
        path: container.path,
        label: container.title || container.id,
        icon: container.icon,
        order: container.order ?? 60,
        source: 'contributes',
      })
    }
  }
}

/**
 * 获取所有导航项（默认 + 动态注册 + contributes）
 */
export function getNavItems(): NavItem[] {
  const all = [...DEFAULT_NAV_ITEMS, ...dynamicNavItems]
  return all.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
}

/**
 * 获取当前用户可见的导航项
 */
export function getVisibleNavItems(isAdmin: boolean = false): NavItem[] {
  return getNavItems().filter((item) => !item.adminOnly || isAdmin)
}
