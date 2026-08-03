/**
 * 内核设置导航项共享数据源
 *
 * SettingsPage（全屏路由页 /settings）与 SettingsHubWidget（工作区设置面板
 * settings_hub）都需要展示「内核设置」导航（主题 / 插件 / 管道）。
 *
 * 此前两处各自维护一份清单（SettingsPage.BUILTIN_ITEMS 与
 * SettingsHubWidget.KERNEL_NAV），同一业务概念两处定义（散点），新增内核项需双修。
 * 统一收拢到此处，两处消费同一数据源。
 */

export interface KernelNavItem {
  /** 稳定 id（SettingsPage 的 builtin item id 使用） */
  id: string
  /** 完整标题（SettingsPage 全屏页左侧导航展示） */
  title: string
  /** 短标签（SettingsHubWidget 工作区面板展示） */
  label: string
  /** 描述文案 */
  description: string
  /** 图标（emoji） */
  icon: string
  /** 分组（均为内核设置） */
  group: '内核'
}

export const KERNEL_NAV_ITEMS: KernelNavItem[] = [
  {
    id: 'theme',
    title: '主题设置',
    label: '主题',
    description: '切换界面主题和显示模式',
    icon: '🎨',
    group: '内核',
  },
  {
    id: 'plugins',
    title: '插件管理',
    label: '插件注册表',
    description: '启用/禁用插件、查看状态',
    icon: '🔌',
    group: '内核',
  },
  {
    id: 'pipeline',
    title: '管道配置',
    label: '管道配置',
    description: '管理管道插件链与 Agent 管道配置',
    icon: '🔀',
    group: '内核',
  },
]
