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
  /** 设置页面 */
  SETTINGS: '/settings',
  /** 设置子页面 */
  SETTINGS_API: '/settings/api',
  SETTINGS_LLM: '/settings/llm',
  SETTINGS_CONTEXT: '/settings/context',
  SETTINGS_CONCURRENCY: '/settings/concurrency',
  SETTINGS_COST: '/settings/cost',
  /** 工具页面 */
  TOOLS: '/tools',
  /** 智能体页面 */
  AGENTS: '/agents',
  /** 监控页面 */
  MONITORING: '/monitoring',
  /** 管理员页面 */
  ADMIN: '/admin',
  /** 记忆页面 */
  MEMORY: '/memory',
  /** 会话页面 */
  SESSION: (id: string) => `/session/${id}`,
  /** 演示页面 */
  DEMO: {
    DEEP_SPACE: '/demo/deep-space',
  },
  /** 测试页面 */
  TEST: {
    TOOL_CARDS: '/test/tool-cards',
  },
  /** 调试页面 */
  DEBUG: {
    /** 调试中心入口 */
    ROOT: '/debug',
    /** 执行记录 */
    EXECUTION_RECORDS: '/debug/execution-records',
    /** 会话 */
    SESSIONS: '/debug/sessions',
    /** 任务 */
    TASKS: '/debug/tasks',
    /** 评估指标 */
    EVALUATION_METRICS: '/debug/evaluation-metrics',
    /** 用户 */
    USERS: '/debug/users',
  },
} as const

/**
 * 路由路径类型
 */
export type RoutePath = (typeof ROUTES)[keyof typeof ROUTES]
