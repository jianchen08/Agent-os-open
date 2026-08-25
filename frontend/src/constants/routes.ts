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
  /** 管理员页面 */
  ADMIN: '/admin',
  /** 记忆页面 */
  MEMORY: '/memory',
  /** 知识库页面 */
  KNOWLEDGE_BASE: '/knowledge-base',
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
    /** 数据库管理（统一数据接口 /api/v1/db/*） */
    DB: '/debug/db',
    /** LLM 请求快照（最近发送给大模型的真实请求体） */
    LLM_PAYLOAD: '/debug/llm-payload',
  },
} as const
