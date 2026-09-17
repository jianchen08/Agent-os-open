/**
 * 路由路径常量定义
 */

/**
 * 应用路由路径
 */
export const ROUTES = {
  /** 首页/主界面（统一使用 HOME） */
  HOME: '/',
  /** 登录页 */
  LOGIN: '/login',
  /** 注册页 */
  REGISTER: '/register',
  // /settings 路由族无独立路由页（设置工作区页签化）：设置唯一入口 =
  // openWorkspacePanelByPath('/settings')（SettingsHubWidget）。
  // /tools、/agents 无独立路由页（agent_manager 插件化）：
  // 智能体页面由 agent_manager 插件 contributes.pages 声明（path=/agents，
  // 经 openWorkspacePanelByPath 解析）；能力浏览并入设置中枢「插件注册表」。
  // /admin 路由已退役：用户管理 = user_admin 插件声明页（widget_stage 组台）。
  // /memory、/knowledge-base 路由已退役：hindsight_memory 插件声明页
  // （「记忆」单页 = 对话记忆 + 文档库两分区，/p/memory 经通配路由全页渲染）。
  // /debug 路由族已退役：调试中心 = debug_center 插件声明页（debug_center_hub
  // 工作区面板内嵌九个子页），无独立路由。
} as const
