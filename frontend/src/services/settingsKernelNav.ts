/**
 * 内核设置导航项共享数据源
 *
 * SettingsPage（全屏路由页 /settings）与 SettingsHubWidget（工作区设置面板
 * settings_hub）都需要展示「内核设置」导航（主题 / 插件 / 管道）。
 *
 * 统一收拢到此处单一数据源，两处消费同一数据源——避免同一业务概念两处定义
 * （各自维护清单时新增内核项需双修）。
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
    id: 'llm',
    title: '模型设置',
    label: '模型',
    description: '配置大语言模型提供商、密钥与模型',
    icon: '🤖',
    group: '内核',
  },
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
    id: 'api',
    title: 'API 设置',
    label: 'API',
    description: '配置 API 端点与访问凭证',
    icon: '🛠️',
    group: '内核',
  },
  {
    id: 'concurrency',
    title: '并发设置',
    label: '并发',
    description: '配置请求并发数与限流策略',
    icon: '⚙️',
    group: '内核',
  },
]
