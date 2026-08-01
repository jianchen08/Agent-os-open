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
  /** 记忆管理相关 - 4c 迁移：已切 /ext/channel_api/memory/*（进程态 store stopgap） */
  MEMORY: {
    /** 获取情景记忆列表 */
    EPISODES: '/ext/channel_api/memory/episodes',
    /** 获取单个情景记忆 */
    EPISODE: (id: string) => `/ext/channel_api/memory/episodes/${id}`,
    /** 搜索记忆 */
    SEARCH: '/ext/channel_api/memory/search',
    /** 获取语义记忆列表 */
    SEMANTIC: '/ext/channel_api/memory/semantic',
    /** 记忆整合 */
    CONSOLIDATE: '/ext/channel_api/memory/consolidate',
    /** 获取记忆统计 */
    STATS: '/ext/channel_api/memory/stats',
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
  /** 配置管理相关 - 4c 迁移：已切 /ext/channel_api/config/**（经内核 dispatcher → channel_api http.handle） */
  CONFIG: {
    /** 获取 API 配置 */
    API_GET: '/ext/channel_api/config/api',
    /** 更新 API 配置 */
    API_UPDATE: '/ext/channel_api/config/api',
    /** 获取 LLM 配置 */
    LLM_GET: '/ext/channel_api/config/llm',
    /** 获取提供商列表 */
    LLM_PROVIDERS: '/ext/channel_api/config/llm/providers',
    /** 获取模型列表 */
    LLM_MODELS: '/ext/channel_api/config/llm/models',
    /** 获取默认配置 */
    LLM_DEFAULTS: '/ext/channel_api/config/llm/defaults',
    /** 获取上下文窗口配置 */
    CONTEXT_WINDOW_GET: '/ext/channel_api/config/context-window',
    /** 更新上下文窗口配置 */
    CONTEXT_WINDOW_UPDATE: '/ext/channel_api/config/context-window',
    /** 重置上下文窗口配置 */
    CONTEXT_WINDOW_RESET: '/ext/channel_api/config/context-window/reset',
    /** 获取并发配置 */
    CONCURRENCY_GET: '/ext/channel_api/config/concurrency',
    /** 更新并发配置 */
    CONCURRENCY_UPDATE: '/ext/channel_api/config/concurrency',
    /** 获取成本控制配置 */
    COST_CONTROL_GET: '/ext/channel_api/config/cost-control',
    /** 更新成本控制配置 */
    COST_CONTROL_UPDATE: '/ext/channel_api/config/cost-control',
    /** 通用配置（动态路径） */
    GENERIC_GET: (path: string) => `/ext/channel_api/config/generic/${path}`,
    GENERIC_UPDATE: (path: string) => `/ext/channel_api/config/generic/${path}`,
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
  /** 评估指标 - 走插件 http_endpoints /ext/evaluation_service/**（已从内核 compat_routes 迁出）。
   *  指标定义读 evaluation 插件 config_files（config/evaluation/evaluation_metrics.yaml 唯一真相源）。 */
  EVALUATION: {
    /** 获取评估指标列表 */
    METRICS: '/ext/evaluation_service/metrics',
    /** 获取单个评估指标 */
    METRIC: (id: string) => `/ext/evaluation_service/metrics/${id}`,
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
  /** 用户设置相关 - 4c 迁移：已切 /ext/channel_api/users/settings（stub） */
  USER_SETTINGS: {
    /** 获取用户设置 */
    GET: '/ext/channel_api/users/settings',
    /** 更新用户设置 */
    UPDATE: '/ext/channel_api/users/settings',
  },
  /** 用户管理相关 - 4c 迁移：已切 /ext/channel_api/users/*（管理员专用，stub） */
  USERS: {
    /** 获取用户列表 */
    LIST: '/ext/channel_api/users',
    /** 获取用户统计 */
    STATS: '/ext/channel_api/users/stats',
    /** 创建用户 */
    CREATE: '/ext/channel_api/users',
    /** 更新用户角色 */
    UPDATE_ROLE: (id: string) => `/ext/channel_api/users/${id}/role`,
    /** 更新用户激活状态 */
    UPDATE_ACTIVE: (id: string) => `/ext/channel_api/users/${id}/active`,
    /** 删除用户 */
    DELETE: (id: string) => `/ext/channel_api/users/${id}`,
  },
  /** 监控相关 - 走插件 http_endpoints /ext/monitoring/**（已从内核 compat_routes 迁出） */
  MONITORING: {
    /** 获取系统指标 */
    SYSTEM_METRICS: '/ext/monitoring/system/metrics',
    /** 获取任务统计 */
    TASK_STATISTICS: '/ext/monitoring/tasks/statistics',
    /** 获取任务列表 */
    TASK_LIST: '/ext/monitoring/tasks',
    /** 获取 Token 使用统计 */
    TOKEN_USAGE: '/ext/monitoring/token-usage',
    /** 获取缓存命中率统计 */
    CACHE_STATS: '/ext/monitoring/cache-stats',
  },
  /** 任务管理 - 对应后端 /api/v1/tasks/* */
  TASKS: {
    /** 获取任务列表 */
    LIST: '/ext/channel_api/tasks',
    /** 创建任务 */
    CREATE: '/ext/channel_api/tasks',
    /** 手动创建根任务（用户以 L1 身份发起，为 L2+ 提供 task 上下文） */
    CREATE_ROOT: '/ext/channel_api/tasks/root',
    /** 列出会话的容器任务（供新建子任务选父容器） */
    CONTAINERS: '/ext/channel_api/tasks/containers',
    /** 获取任务详情 */
    GET: (id: string) => `/ext/channel_api/tasks/${id}`,
    /** 更新任务 */
    UPDATE: (id: string) => `/ext/channel_api/tasks/${id}`,
    /** 删除任务 */
    DELETE: (id: string) => `/ext/channel_api/tasks/${id}`,
    /** 获取评估状态（注：后端无对应路由，前端未使用，dead constant） */
    EVALUATION_STATUS: (id: string) => `/ext/channel_api/tasks/${id}/evaluation-status`,
    /** 暂停任务（级联子任务） */
    PAUSE: (id: string) => `/ext/channel_api/tasks/${id}/pause`,
    /** 恢复任务（级联子任务） */
    RESUME: (id: string) => `/ext/channel_api/tasks/${id}/resume`,
    /** 取消任务 */
    CANCEL: (id: string) => `/ext/channel_api/tasks/${id}/cancel`,
  },
  /** 长期任务相关 - 4c/批次3 迁移：已切 /ext/channel_api/projects/* */
  PROJECTS: {
    /** 获取长期任务列表 */
    LIST: '/ext/channel_api/projects',
    /** 创建长期任务 */
    CREATE: '/ext/channel_api/projects',
    /** 获取长期任务详情 */
    GET: (id: string) => `/ext/channel_api/projects/${id}`,
    /** 切换自动执行开关 */
    TOGGLE_AUTO_EXECUTE: (id: string) => `/ext/channel_api/projects/${id}/auto-execute`,
    /** 暂停长期任务 */
    PAUSE: (id: string) => `/ext/channel_api/projects/${id}/pause`,
    /** 恢复长期任务 */
    RESUME: (id: string) => `/ext/channel_api/projects/${id}/resume`,
    /** 删除长期任务 */
    DELETE: (id: string) => `/ext/channel_api/projects/${id}`,
  },
  /** 任务阶段相关 - 4c/批次3 迁移：已切 /ext/channel_api/tasks/{id}/phase/* */
  TASK_PHASES: {
    /** 获取任务阶段状态 */
    GET_STATUS: (taskId: string) => `/ext/channel_api/tasks/${taskId}/phase`,
    /** 完成准备阶段 */
    COMPLETE_PREPARE: (taskId: string) => `/ext/channel_api/tasks/${taskId}/phase/prepare/complete`,
    /** 完成执行阶段 */
    COMPLETE_EXECUTE: (taskId: string) => `/ext/channel_api/tasks/${taskId}/phase/execute/complete`,
    /** 获取阶段产物 */
    GET_OUTPUT: (taskId: string, phase: string) => `/ext/channel_api/tasks/${taskId}/phase/${phase}/output`,
  },
  /** 任务验收标准评估相关 - 4c/批次3 迁移：已切 /ext/channel_api/tasks/{id}/ac/* */
  TASK_EVALUATION: {
    /** 获取任务所有验收标准 */
    LIST: (taskId: string) => `/ext/channel_api/tasks/${taskId}/ac`,
    /** 评估单个验收标准 */
    EVALUATE: (taskId: string, acId: string) => `/ext/channel_api/tasks/${taskId}/ac/${acId}/evaluate`,
    /** 评估所有验收标准 */
    EVALUATE_ALL: (taskId: string) => `/ext/channel_api/tasks/${taskId}/ac/evaluate-all`,
    /** 获取验收标准评估结果 */
    GET_RESULT: (taskId: string, acId: string) => `/ext/channel_api/tasks/${taskId}/ac/${acId}/result`,
  },
  /** 思考模式相关 - 4c 迁移：已切 /ext/channel_api/thinking-mode/**（经内核 dispatcher → channel_api http.handle） */
  THINKING_MODE: {
    /** 测试端点（注：后端无对应路由，仅前端定义） */
    TEST: '/ext/channel_api/thinking-mode/test',
    /** 获取所有支持思考模式的模型 */
    MODELS: '/ext/channel_api/thinking-mode/models',
    /** 获取指定模型的思考模式信息 */
    MODEL_INFO: (modelName: string) => `/ext/channel_api/thinking-mode/models/${modelName}`,
    /** 使用思考模式生成响应（注：后端无对应路由，仅前端定义） */
    GENERATE: '/ext/channel_api/thinking-mode/generate',
    /** 切换思考模式 */
    SWITCH: '/ext/channel_api/thinking-mode/switch',
    /** 获取思考模式推荐 */
    RECOMMENDATIONS: '/ext/channel_api/thinking-mode/recommendations',
    /** 检查模型是否支持思考模式 */
    CHECK_SUPPORT: (modelName: string) => `/ext/channel_api/thinking-mode/check/${modelName}`,
    /** 思考模式服务健康检查 */
    HEALTH: '/ext/channel_api/thinking-mode/health',
  },
  /** 成本控制相关 - 走插件 http_endpoints /ext/cost_control/**（已从内核 compat_routes 迁出） */
  COST_CONTROL: {
    /** 获取预算状态 */
    BUDGET_STATUS: '/ext/cost_control/budget/status',
    /** 获取使用统计 */
    USAGE_STATISTICS: '/ext/cost_control/usage/statistics',
    /** 获取成本配置 */
    CONFIG: '/ext/cost_control/config',
    /** 获取成本报表 */
    REPORT: '/ext/cost_control/report',
    /** 重置预算 */
    BUDGET_RESET: '/ext/cost_control/budget/reset',
  },
  /** Schema 聚合相关 - 对应后端 /api/v1/schema 端点（聚合 agents/pipelines/tools/ui_schema） */
  SCHEMA: {
    /** 获取聚合 Schema（含插件 ui_schema 声明） */
    GET: '/api/v1/schema',
  },
  /** 插件配置相关 - 对应后端 /api/v1/plugins/{id}/config/{file_id} 端点（ADR §4.3） */
  PLUGIN_CONFIG: {
    /** 取/存某个插件的某个配置文件；需填充 pluginId 与 fileId */
    FILE: (pluginId: string, fileId: string) => `/api/v1/plugins/${pluginId}/config/${fileId}`,
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
    LIST: '/ext/channel_api/triggers',
    /** 获取触发器统计 */
    STATS: '/ext/channel_api/triggers/stats',
    /** 获取触发器详情 */
    GET: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}`,
    /** 创建触发器 */
    CREATE: '/ext/channel_api/triggers',
    /** 更新触发器 */
    UPDATE: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}`,
    /** 删除触发器 */
    DELETE: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}`,
    /** 启用触发器 */
    ENABLE: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}/enable`,
    /** 禁用触发器 */
    DISABLE: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}/disable`,
    /** 手动触发触发器 */
    TRIGGER: (triggerId: string) => `/ext/channel_api/triggers/${triggerId}/trigger`,
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
  /** 数据清理相关 - 4c 迁移：已切 /ext/channel_api/execution/records/clear-all */
  DATA_CLEANUP: {
    /** 一键清理所有会话和执行记录 */
    CLEAR_ALL: '/ext/channel_api/execution/records/clear-all',
  },
  /** 人类交互相关 - 4c/批次5 迁移：已切 /ext/channel_api/interaction/* */
  INTERACTION: {
    SUBMIT_RESPONSE: '/ext/channel_api/interaction/response',
    APPROVE: (requestId: string) => `/ext/channel_api/interaction/${requestId}/approve`,
    DENY: (requestId: string) => `/ext/channel_api/interaction/${requestId}/deny`,
    CANCEL: (requestId: string) => `/ext/channel_api/interaction/${requestId}/cancel`,
    VIEWED: (requestId: string) => `/ext/channel_api/interaction/${requestId}/viewed`,
    PENDING: '/ext/channel_api/interaction/pending',
    GET: (requestId: string) => `/ext/channel_api/interaction/${requestId}`,
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
