/**
 * API端点常量定义
 *
 * 与后端API端点对齐，确保前后端一致性。
 * Requirements: 1.1, 1.2, 1.4, 1.5
 */

/**
 * API基础URL（从环境变量读取，空值时使用相对路径由Vite代理转发）
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

/**
 * API端点路径
 *
 * 所有端点路径与后端FastAPI路由对齐：
 * - 认证端点：/api/v1/auth/*
 * - 线程端点：/api/v1/threads/*
 * - 记忆端点：/api/v1/memory/*
 * - 评估端点：/api/v1/evaluation/*
 */
export const API_ENDPOINTS = {
  /** 认证相关 - 对应后端 /api/v1/auth/* */
  AUTH: {
    /** 登录 */
    LOGIN: '/api/v1/auth/login',
    /** 注册 */
    REGISTER: '/api/v1/auth/register',
    /** 刷新令牌 */
    REFRESH_TOKEN: '/api/v1/auth/refresh',
    /** 登出 */
    LOGOUT: '/api/v1/auth/logout',
    /** 获取当前用户信息 */
    ME: '/api/v1/auth/me',
  },
  /** 线程/会话相关 - 对应后端 /api/v1/threads/* */
  THREADS: {
    /** 获取线程列表 */
    LIST: '/api/v1/threads',
    /** 创建线程 */
    CREATE: '/api/v1/threads',
    /** 获取线程详情 */
    GET: (id: string) => `/api/v1/threads/${id}`,
    /** 删除线程 */
    DELETE: (id: string) => `/api/v1/threads/${id}`,
    /** 更新线程 - Requirements: 6.2 */
    UPDATE: (id: string) => `/api/v1/threads/${id}`,
    /** 更新会话绑定的 Agent */
    UPDATE_AGENT: (id: string) => `/api/v1/threads/${id}/agent`,
  },
  /** 消息相关 - 对应后端 /api/v1/threads/{id}/messages */
  MESSAGES: {
    /** 获取线程消息列表（从数据库ExecutionRecord表读取执行记录） */
    LIST: (threadId: string) => `/api/v1/threads/${threadId}/messages`,
    /** 发送消息（通过 WebSocket 发送，此端点仅用于历史消息） */
    SEND: (threadId: string) => `/api/v1/threads/${threadId}/messages`,
  },
  /** 记忆管理相关 - 对应后端 /api/v1/memory/* */
  MEMORY: {
    /** 获取情景记忆列表 */
    EPISODES: '/api/v1/memory/episodes',
    /** 获取单个情景记忆 */
    EPISODE: (id: string) => `/api/v1/memory/episodes/${id}`,
    /** 搜索记忆 */
    SEARCH: '/api/v1/memory/search',
    /** 获取语义记忆列表 */
    SEMANTIC: '/api/v1/memory/semantic',
    /** 记忆整合 */
    CONSOLIDATE: '/api/v1/memory/consolidate',
    /** 获取记忆统计 */
    STATS: '/api/v1/memory/stats',
  },
  /** Agent配置相关 - 对应后端 /api/v1/agents/* */
  AGENTS: {
    /** 获取Agent配置列表 */
    LIST: '/api/v1/agents',
    /** 获取单个Agent配置 */
    GET: (id: string) => `/api/v1/agents/${id}`,
    /** 创建Agent配置 */
    CREATE: '/api/v1/agents',
    /** 更新Agent配置 */
    UPDATE: (id: string) => `/api/v1/agents/${id}`,
    /** 删除Agent配置 */
    DELETE: (id: string) => `/api/v1/agents/${id}`,
    /** Agent健康检查 */
    HEALTH: '/api/v1/agents/health',
    /** 获取默认Agent */
    DEFAULT: '/api/v1/agents/default',
  },
  /** 配置管理相关 - 对应后端 /api/v1/config/* */
  CONFIG: {
    /** 获取 API 配置 */
    API_GET: '/api/v1/config/api',
    /** 更新 API 配置 */
    API_UPDATE: '/api/v1/config/api',
    /** 获取 LLM 配置 */
    LLM_GET: '/api/v1/config/llm',
    /** 获取提供商列表 */
    LLM_PROVIDERS: '/api/v1/config/llm/providers',
    /** 获取模型列表 */
    LLM_MODELS: '/api/v1/config/llm/models',
    /** 获取默认配置 */
    LLM_DEFAULTS: '/api/v1/config/llm/defaults',
    /** 获取上下文窗口配置 */
    CONTEXT_WINDOW_GET: '/api/v1/config/context-window',
    /** 更新上下文窗口配置 */
    CONTEXT_WINDOW_UPDATE: '/api/v1/config/context-window',
    /** 重置上下文窗口配置 */
    CONTEXT_WINDOW_RESET: '/api/v1/config/context-window/reset',
    /** 获取并发配置 */
    CONCURRENCY_GET: '/api/v1/config/concurrency',
    /** 更新并发配置 */
    CONCURRENCY_UPDATE: '/api/v1/config/concurrency',
    /** 获取成本控制配置 */
    COST_CONTROL_GET: '/api/v1/config/cost-control',
    /** 更新成本控制配置 */
    COST_CONTROL_UPDATE: '/api/v1/config/cost-control',
    /** 通用配置（动态路径） */
    GENERIC_GET: (path: string) => `/api/v1/config/generic/${path}`,
    GENERIC_UPDATE: (path: string) => `/api/v1/config/generic/${path}`,
  },
  /** 工具相关 - 对应后端 /api/v1/tools/* */
  TOOLS: {
    /** 生成工具 */
    GENERATE: '/api/v1/tools/generate',
    /** 获取工具详情 */
    GET: (id: string) => `/api/v1/tools/${id}`,
    /** 获取工具列表 */
    LIST: '/api/v1/tools',
    /** 更新工具 */
    UPDATE: (id: string) => `/api/v1/tools/${id}`,
    /** 回滚工具版本 */
    ROLLBACK: (id: string) => `/api/v1/tools/${id}/rollback`,
    /** 删除工具 */
    DELETE: (id: string) => `/api/v1/tools/${id}`,
    /** 获取代码条目 */
    CODE: (id: string) => `/api/v1/tools/code/${id}`,
    /** 搜索代码 */
    CODE_SEARCH: '/api/v1/tools/code',
    /** 获取Agent配置 */
    AGENT_CONFIG: (id: string) => `/api/v1/tools/agent-config/${id}`,
    /** 执行Agent */
    AGENT_EXECUTE: '/api/v1/tools/agent/execute',
  },
  /** 评估相关 - 对应后端 /api/v1/evaluation/* */
  EVALUATION: {
    /** 执行评估 */
    EVALUATE: '/api/v1/evaluation/evaluate',
    /** 获取评估配置列表 */
    PROFILES: '/api/v1/evaluation/profiles',
    /** 获取单个评估配置 */
    PROFILE: (id: string) => `/api/v1/evaluation/profiles/${id}`,
    /** 获取默认评估配置 */
    DEFAULT_PROFILE: '/api/v1/evaluation/profiles/default',
    /** 设置默认评估配置 */
    SET_DEFAULT: (id: string) => `/api/v1/evaluation/profiles/${id}/set-default`,
    /** 获取评估报告 */
    REPORT: (id: string) => `/api/v1/evaluation/reports/${id}`,
    /** 获取评估报告列表 */
    REPORTS: '/api/v1/evaluation/reports',
    /** 获取评估指标列表 */
    METRICS: '/api/v1/evaluation-metrics',
    /** 获取单个评估指标 */
    METRIC: (id: string) => `/api/v1/evaluation-metrics/${id}`,
    /** 获取评估统计 */
    STATISTICS: '/api/v1/evaluation/statistics',
    /** 获取评估趋势 */
    TRENDS: '/api/v1/evaluation/trends',
  },
  /** 健康检查相关 */
  HEALTH: {
    /** 健康检查 */
    CHECK: '/health',
    /** 存活检查 */
    LIVE: '/health/live',
    /** 就绪检查 */
    READY: '/health/ready',
  },
  /** 用户设置相关 - 对应后端 /api/v1/users/settings */
  USER_SETTINGS: {
    /** 获取用户设置 */
    GET: '/api/v1/users/settings',
    /** 更新用户设置 */
    UPDATE: '/api/v1/users/settings',
  },
  /** 用户管理相关 - 对应后端 /api/v1/users/* (管理员专用) */
  USERS: {
    /** 获取用户列表 */
    LIST: '/api/v1/users',
    /** 获取用户统计 */
    STATS: '/api/v1/users/stats',
    /** 创建用户 */
    CREATE: '/api/v1/users',
    /** 更新用户角色 */
    UPDATE_ROLE: (id: string) => `/api/v1/users/${id}/role`,
    /** 更新用户激活状态 */
    UPDATE_ACTIVE: (id: string) => `/api/v1/users/${id}/active`,
    /** 删除用户 */
    DELETE: (id: string) => `/api/v1/users/${id}`,
  },
  /** 监控相关 - 对应后端 /api/v1/monitoring/* */
  MONITORING: {
    /** 获取系统指标 */
    SYSTEM_METRICS: '/api/v1/monitoring/system/metrics',
    /** 获取任务统计 */
    TASK_STATISTICS: '/api/v1/monitoring/tasks/statistics',
    /** 获取任务列表 */
    TASK_LIST: '/api/v1/monitoring/tasks',
    /** 获取事件列表 */
    EVENT_LIST: '/api/v1/monitoring/events',
    /** 获取 Token 使用统计 */
    TOKEN_USAGE: '/api/v1/monitoring/token-usage',
    /** 获取缓存命中率统计 */
    CACHE_STATS: '/api/v1/monitoring/cache-stats',
  },
  /** 任务管理 - 对应后端 /api/v1/tasks/* */
  TASKS: {
    /** 获取任务列表 */
    LIST: '/api/v1/tasks',
    /** 创建任务 */
    CREATE: '/api/v1/tasks',
    /** 手动创建根任务（用户以 L1 身份发起，为 L2+ 提供 task 上下文） */
    CREATE_ROOT: '/api/v1/tasks/root',
    /** 列出会话的容器任务（供新建子任务选父容器） */
    CONTAINERS: '/api/v1/tasks/containers',
    /** 获取任务详情 */
    GET: (id: string) => `/api/v1/tasks/${id}`,
    /** 更新任务 */
    UPDATE: (id: string) => `/api/v1/tasks/${id}`,
    /** 删除任务 */
    DELETE: (id: string) => `/api/v1/tasks/${id}`,
    /** 获取评估状态 */
    EVALUATION_STATUS: (id: string) => `/api/v1/tasks/${id}/evaluation-status`,
    /** 暂停任务（级联子任务） */
    PAUSE: (id: string) => `/api/v1/tasks/${id}/pause`,
    /** 恢复任务（级联子任务） */
    RESUME: (id: string) => `/api/v1/tasks/${id}/resume`,
    /** 取消任务 */
    CANCEL: (id: string) => `/api/v1/tasks/${id}/cancel`,
  },
  /** 任务执行闭环相关 - 对应后端 /api/v1/projects/* 和 /api/v1/tasks/* */
  PROJECTS: {
    /** 获取长期任务列表 */
    LIST: '/api/v1/projects',
    /** 创建长期任务 */
    CREATE: '/api/v1/projects',
    /** 获取长期任务详情 */
    GET: (id: string) => `/api/v1/projects/${id}`,
    /** 切换自动执行开关 */
    TOGGLE_AUTO_EXECUTE: (id: string) => `/api/v1/projects/${id}/auto-execute`,
    /** 暂停长期任务 */
    PAUSE: (id: string) => `/api/v1/projects/${id}/pause`,
    /** 恢复长期任务 */
    RESUME: (id: string) => `/api/v1/projects/${id}/resume`,
    /** 删除长期任务 */
    DELETE: (id: string) => `/api/v1/projects/${id}`,
  },
  /** 任务阶段相关 - 对应后端 /api/v1/tasks/{id}/phase/* */
  TASK_PHASES: {
    /** 获取任务阶段状态 */
    GET_STATUS: (taskId: string) => `/api/v1/tasks/${taskId}/phase`,
    /** 完成准备阶段 */
    COMPLETE_PREPARE: (taskId: string) => `/api/v1/tasks/${taskId}/phase/prepare/complete`,
    /** 完成执行阶段 */
    COMPLETE_EXECUTE: (taskId: string) => `/api/v1/tasks/${taskId}/phase/execute/complete`,
    /** 获取阶段产物 */
    GET_OUTPUT: (taskId: string, phase: string) => `/api/v1/tasks/${taskId}/phase/${phase}/output`,
  },
  /** 任务评估相关 - 对应后端 /api/v1/tasks/{id}/ac/* */
  TASK_EVALUATION: {
    /** 获取任务所有验收标准 */
    LIST: (taskId: string) => `/api/v1/tasks/${taskId}/ac`,
    /** 评估单个验收标准 */
    EVALUATE: (taskId: string, acId: string) => `/api/v1/tasks/${taskId}/ac/${acId}/evaluate`,
    /** 评估所有验收标准 */
    EVALUATE_ALL: (taskId: string) => `/api/v1/tasks/${taskId}/ac/evaluate-all`,
    /** 获取验收标准评估结果 */
    GET_RESULT: (taskId: string, acId: string) => `/api/v1/tasks/${taskId}/ac/${acId}/result`,
  },
  /** 思考模式相关 - 对应后端 /api/v1/thinking-mode/* */
  THINKING_MODE: {
    /** 测试端点 */
    TEST: '/api/v1/thinking-mode/test',
    /** 获取所有支持思考模式的模型 */
    MODELS: '/api/v1/thinking-mode/models',
    /** 获取指定模型的思考模式信息 */
    MODEL_INFO: (modelName: string) => `/api/v1/thinking-mode/models/${modelName}`,
    /** 使用思考模式生成响应 */
    GENERATE: '/api/v1/thinking-mode/generate',
    /** 切换思考模式 */
    SWITCH: '/api/v1/thinking-mode/switch',
    /** 获取思考模式推荐 */
    RECOMMENDATIONS: '/api/v1/thinking-mode/recommendations',
    /** 检查模型是否支持思考模式 */
    CHECK_SUPPORT: (modelName: string) => `/api/v1/thinking-mode/check/${modelName}`,
    /** 思考模式服务健康检查 */
    HEALTH: '/api/v1/thinking-mode/health',
  },
  /** 主题管理 - 无状态清单接口（后端只扫目录返回元数据，主题内容仍归前端） */
  THEMES: {
    /** 动态主题清单（扫描 public/themes/*.json，返回 id/name/url） */
    MANIFEST: '/api/v1/themes/manifest',
  },
  /** 成本控制相关 - 对应后端 /api/v1/cost-control/* */
  COST_CONTROL: {
    /** 获取预算状态 */
    BUDGET_STATUS: '/api/v1/cost-control/budget/status',
    /** 获取使用统计 */
    USAGE_STATISTICS: '/api/v1/cost-control/usage/statistics',
    /** 获取成本配置 */
    CONFIG: '/api/v1/cost-control/config',
    /** 获取成本报表 */
    REPORT: '/api/v1/cost-control/report',
    /** 重置预算 */
    BUDGET_RESET: '/api/v1/cost-control/budget/reset',
  },
  /** 悬浮窗相关 - 对应后端 /api/v1/floating-chat/* */
  FLOATING_CHAT: {
    /** 获取悬浮窗状态 */
    STATUS: '/api/v1/floating-chat/status',
    /** 启动悬浮窗 */
    LAUNCH: '/api/v1/floating-chat/launch',
  },
  /** 触发器相关 - 对应后端 /api/v1/triggers/* */
  TRIGGERS: {
    /** 获取触发器列表 */
    LIST: '/api/v1/triggers',
    /** 获取触发器统计 */
    STATS: '/api/v1/triggers/stats',
    /** 获取触发器详情 */
    GET: (triggerId: string) => `/api/v1/triggers/${triggerId}`,
    /** 创建触发器 */
    CREATE: '/api/v1/triggers',
    /** 更新触发器 */
    UPDATE: (triggerId: string) => `/api/v1/triggers/${triggerId}`,
    /** 删除触发器 */
    DELETE: (triggerId: string) => `/api/v1/triggers/${triggerId}`,
    /** 启用触发器 */
    ENABLE: (triggerId: string) => `/api/v1/triggers/${triggerId}/enable`,
    /** 禁用触发器 */
    DISABLE: (triggerId: string) => `/api/v1/triggers/${triggerId}/disable`,
    /** 手动触发触发器 */
    TRIGGER: (triggerId: string) => `/api/v1/triggers/${triggerId}/trigger`,
  },
  /** Agent 调用记录相关 - 对应后端 /api/v1/agent-calls/* */
  AGENT_CALLS: {
    /** 获取调用记录列表 */
    LIST: '/api/v1/agent-calls',
    /** 获取调用统计 */
    STATISTICS: '/api/v1/agent-calls/statistics',
    /** 获取调用记录详情 */
    GET: (executionId: string) => `/api/v1/agent-calls/${executionId}`,
  },
  /** 数据清理相关 - 对应后端 /api/v1/execution/records/* */
  DATA_CLEANUP: {
    /** 一键清理所有会话和执行记录 */
    CLEAR_ALL: '/api/v1/execution/records/clear-all',
  },
  /** 人类交互相关 - 对应后端 /api/v1/interaction/* */
  INTERACTION: {
    SUBMIT_RESPONSE: '/api/v1/interaction/response',
    APPROVE: (requestId: string) => `/api/v1/interaction/${requestId}/approve`,
    DENY: (requestId: string) => `/api/v1/interaction/${requestId}/deny`,
    CANCEL: (requestId: string) => `/api/v1/interaction/${requestId}/cancel`,
    VIEWED: (requestId: string) => `/api/v1/interaction/${requestId}/viewed`,
    PENDING: '/api/v1/interaction/pending',
    GET: (requestId: string) => `/api/v1/interaction/${requestId}`,
  },
  /** 知识库相关 - 对应后端 /api/v1/knowledge-base/* */
  KNOWLEDGE_BASE: {
    /** 获取知识库列表 */
    LIST: '/api/v1/knowledge-base',
    /** 获取知识库统计 */
    STATS: '/api/v1/knowledge-base/stats',
    /** 上传文件 */
    UPLOAD: '/api/v1/knowledge-base/upload',
    /** 获取知识库详情 */
    GET: (id: string) => `/api/v1/knowledge-base/${id}`,
    /** 删除知识库 */
    DELETE: (id: string) => `/api/v1/knowledge-base/${id}`,
    /** 检查知识库 */
    CHECK: '/api/v1/knowledge-base/check',
    /** 获取分类列表 */
    CATEGORIES: '/api/v1/knowledge-base/categories',
    /** 创建分类 */
    CREATE_CATEGORY: '/api/v1/knowledge-base/categories',
    /** 删除分类 */
    DELETE_CATEGORY: (name: string) => `/api/v1/knowledge-base/categories/${name}`,
    /** 获取标签列表 */
    TAGS: '/api/v1/knowledge-base/tags',
  },
} as const

/**
 * API请求超时时间（毫秒）
 */
export const API_TIMEOUT = 30000

/**
 * API重试次数
 */
export const API_RETRY_COUNT = 3

/**
 * API重试延迟（毫秒）
 */
export const API_RETRY_DELAY = 1000
