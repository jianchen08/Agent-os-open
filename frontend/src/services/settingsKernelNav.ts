/**
 * 内核设置导航项共享数据源
 *
 * SettingsHubWidget（工作区设置面板 settings_hub，设置唯一 UI）的「内核设置」
 * 导航（主题 / 插件 / 管道 / 后端连接 / 通知 / 关于）。无独立路由页 /settings。
 * 模型设置已迁为 llm_service 插件声明的 settings 页（contributes.pages，widget 承载）。
 * 连接/同步/我的地盘已收敛单页（2026-10-18）；设备配对与多端同步并入「后端连接」
 * 页作为区块（合并改造：独立「设备」入口退役，唯一入口=后端连接设置面板）。
 */

export interface KernelNavItem {
  /** 稳定 id（设置中枢 nav key 前缀 kernel-{id}） */
  id: string
  /** 完整标题 */
  title: string
  /** 短标签（设置中枢左侧导航展示） */
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
  {
    id: 'backend-connection',
    title: '后端连接',
    label: '后端连接',
    description: '本机内核档 / 自定义后端 URL 切换、设备配对与多端同步、地盘快照',
    icon: '🌐',
    group: '内核',
  },
  {
    id: 'notifications',
    title: '通知设置',
    label: '通知',
    description: '系统通知权限状态与系统设置入口',
    icon: '🔔',
    group: '内核',
  },
  {
    id: 'about',
    title: '关于',
    label: '关于',
    description: '应用版本、前端构建标识与内核版本',
    icon: 'ℹ️',
    group: '内核',
  },
]
