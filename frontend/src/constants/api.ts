/**
 * API端点常量定义
 *
 * 与后端API端点对齐，确保前后端一致性。
 * Requirements: 1.1, 1.2, 1.4, 1.5
 *
 * 插件端点唯一真值源 = plugin.json http_endpoints 声明（生成物投影），
 * 本文件只 import 生成物投影（endpoints.generated.ts），不手写 /ext 字面量
 * （ADR 2026-08-21 channel_api 退役：前端端点供给模型改生成式）。
 */

import {
  AGENT_MANAGER_ENDPOINTS,
  APPROVAL_SERVICE_ENDPOINTS,
  COST_CONTROL_ENDPOINTS,
  EVALUATION_SERVICE_ENDPOINTS,
  HINDSIGHT_MEMORY_SERVICE_ENDPOINTS,
  LLM_SERVICE_ENDPOINTS,
  MONITORING_ENDPOINTS,
  TASK_SERVICE_ENDPOINTS,
  USER_ADMIN_ENDPOINTS,
} from '../services/api/endpoints.generated'

/**
 * API基础URL（从环境变量读取，空值时使用相对路径由Vite代理转发）
 */
export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

/**
 * API端点路径
 *
 * 所有端点路径与后端FastAPI路由对齐：
 * - 认证端点：/api/v1/auth/*
 * - 会话端点：/api/v1/sessions/*
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
  /** 会话相关 - 对应后端 /api/v1/sessions/*（compat /threads 转正，见 task_kernel_cleanup_and_split 任务 2） */
  SESSIONS: {
    /** 获取会话列表 */
    LIST: '/api/v1/sessions',
    /** 创建会话 */
    CREATE: '/api/v1/sessions',
    /** 删除会话 */
    DELETE: (id: string) => `/api/v1/sessions/${id}`,
    /** 更新会话 - Requirements: 6.2 */
    UPDATE: (id: string) => `/api/v1/sessions/${id}`,
    /** 更新会话绑定的 Agent */
    UPDATE_AGENT: (id: string) => `/api/v1/sessions/${id}/agent`,
    /** 线程创建表单字段 schema（内核聚合 enabled 插件 contributes.thread_fields） */
    SCHEMA: '/api/v1/sessions/schema',
  },
  /** 管道相关 - 运行快照走内核 /api/v1/pipelines/runs（/api/v1/pipelines 为配置清单，勿混用） */
  PIPELINES: {
    /** 管道运行快照列表（统一管道管理数据源） */
    RUNS: '/api/v1/pipelines/runs',
    /** 管道 state 摘要（内存常驻 + checkpoint 兜底；任务树迭代/阶段真值） */
    STATE: '/api/v1/pipelines/state',
    /** 管道插件清单（id/name/version/role/host_type，管道可视化编辑器的插件目录源） */
    CATALOG: '/api/v1/pipelines',
  },
  /** 插件状态 - 内核 /api/v1/plugins（manifests 派生，含 enabled/config_files） */
  PLUGINS: {
    /** 插件状态列表 */
    LIST: '/api/v1/plugins',
  },
  /** 消息相关 - 对应后端 /api/v1/sessions/{id}/messages */
  MESSAGES: {
    /** 获取会话消息列表（从数据库ExecutionRecord表读取执行记录） */
    LIST: (sessionId: string) => `/api/v1/sessions/${sessionId}/messages`,
  },
  /** 记忆管理相关 - hindsight_memory_service 插件端点（生成物投影，原 channel_api memory 域） */
  MEMORY: {
    /** 获取情景记忆列表 */
    EPISODES: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_episodes_list,
    /** 获取单个情景记忆 */
    EPISODE: (id: string) =>
      HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_episode_get.replace('{episode_id}', id),
    /** 搜索记忆 */
    SEARCH: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_search_get,
    /** 获取语义记忆列表 */
    SEMANTIC: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_semantic_list,
    /** 记忆整合 */
    CONSOLIDATE: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_consolidate,
    /** 获取记忆统计 */
    STATS: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_stats,
  },
  /** Agent配置相关 - agent_manager 插件端点（生成物投影；2026-08-20 插件化，原内核 /api/v1/agents* 已删） */
  AGENTS: {
    /** 获取Agent配置列表 */
    LIST: AGENT_MANAGER_ENDPOINTS.agent_manager_list,
    /** Agent 配置字段 Schema（表单驱动，返回 { fields: UIInputFormField[] }） */
    SCHEMA: AGENT_MANAGER_ENDPOINTS.agent_manager_schema,
    /** 读写 Agent 配置 yaml 原文（PUT 写回前后端自动备份 + If-Match 乐观锁） */
    CONFIG: (id: string) => AGENT_MANAGER_ENDPOINTS.agent_manager_get_config.replace('{id}', id),
  },
  /** 配置管理相关 - llm_service/cost_control 插件端点（生成物投影，原 channel_api config 域） */
  CONFIG: {
    /** 获取 LLM 配置 */
    LLM_GET: LLM_SERVICE_ENDPOINTS.config_llm_get,
    /** 获取提供商列表 */
    LLM_PROVIDERS: LLM_SERVICE_ENDPOINTS.config_llm_providers_get,
    /** 获取 litellm 支持的提供者类型清单（随 litellm 升级自动更新） */
    LLM_PROVIDER_TYPES: LLM_SERVICE_ENDPOINTS.config_llm_provider_types_get,
    /** 从提供商 API 实时拉取可用模型（需先配置 Key） */
    LLM_REMOTE_MODELS: (providerId: string) =>
      LLM_SERVICE_ENDPOINTS.config_llm_providers_remote_models_get.replace('{provider_id}', providerId),
    /** 获取模型列表 */
    LLM_MODELS: LLM_SERVICE_ENDPOINTS.config_llm_models_get,
    /** 获取默认配置 */
    LLM_DEFAULTS: LLM_SERVICE_ENDPOINTS.config_llm_defaults_get,
    /** 获取成本控制配置 */
    COST_CONTROL_GET: COST_CONTROL_ENDPOINTS.cost_config_file_get,
    /** 更新成本控制配置 */
    COST_CONTROL_UPDATE: COST_CONTROL_ENDPOINTS.cost_config_file_put,
    /** 管道配置（P7 内核专用端点，config_service denylist 含 pipelines，不走 generic） */
    PIPELINE_GET: (name: string) => `/api/v1/config/pipelines/${name}`,
    PIPELINE_UPDATE: (name: string) => `/api/v1/config/pipelines/${name}`,
  },
  /** 工具相关 - 对应后端 GET /api/v1/tools 列表端点（其余 {id} CRUD 端点后端不存在，2026-08 已连同服务层函数清理） */
  TOOLS: {
    /** 获取工具列表 */
    LIST: '/api/v1/tools',
  },
  /** 评估指标 - evaluation_service 插件端点（生成物投影，已从内核 compat_routes 迁出）。
   *  指标定义读 evaluation 插件 config_files（config/evaluation/evaluation_metrics.yaml 唯一真相源）。 */
  EVALUATION: {
    /** 获取评估指标列表 */
    METRICS: EVALUATION_SERVICE_ENDPOINTS.metrics_list,
    /** 获取单个评估指标 */
    METRIC: (id: string) => EVALUATION_SERVICE_ENDPOINTS.metric_detail.replace('{metric_id}', id),
  },
  /** 用户管理相关 - user_admin 插件端点（生成物投影，原 channel_api users 域，管理员专用） */
  USERS: {
    /** 获取用户列表 */
    LIST: USER_ADMIN_ENDPOINTS.users,
    /** 获取用户统计 */
    STATS: USER_ADMIN_ENDPOINTS.users_stats,
    /** 创建用户 */
    CREATE: USER_ADMIN_ENDPOINTS.users,
    /** 更新用户角色 */
    UPDATE_ROLE: (id: string) =>
      USER_ADMIN_ENDPOINTS.user_role_update.replace('{user_id}', id),
    /** 更新用户激活状态 */
    UPDATE_ACTIVE: (id: string) =>
      USER_ADMIN_ENDPOINTS.user_active_update.replace('{user_id}', id),
    /** 删除用户 */
    DELETE: (id: string) => USER_ADMIN_ENDPOINTS.user_delete.replace('{user_id}', id),
  },
  /** 监控相关 - monitoring 插件端点（生成物投影，已从内核 compat_routes 迁出） */
  MONITORING: {
    /** 获取系统指标 */
    SYSTEM_METRICS: MONITORING_ENDPOINTS.mon_system_metrics,
    /** 获取任务统计 */
    TASK_STATISTICS: MONITORING_ENDPOINTS.mon_task_statistics,
    /** 获取任务列表 */
    TASK_LIST: MONITORING_ENDPOINTS.mon_tasks,
    /** 获取 Token 使用统计 */
    TOKEN_USAGE: MONITORING_ENDPOINTS.mon_token_usage,
    /** 获取缓存命中率统计 */
    CACHE_STATS: MONITORING_ENDPOINTS.mon_cache_stats,
  },
  /** 任务管理 - task_service 插件端点（生成物投影，原 channel_api tasks 域；内核 /api/v1/tasks 无路由） */
  TASKS: {
    /** 获取任务列表 */
    LIST: TASK_SERVICE_ENDPOINTS.tasks_list,
    /** 创建任务 */
    CREATE: TASK_SERVICE_ENDPOINTS.tasks_create,
    /** 手动创建根任务（用户以 L1 身份发起，为 L2+ 提供 task 上下文） */
    CREATE_ROOT: TASK_SERVICE_ENDPOINTS.tasks_create_root,
    /** 列出会话的容器任务（供新建子任务选父容器） */
    CONTAINERS: TASK_SERVICE_ENDPOINTS.tasks_containers,
    /** 获取任务详情 */
    GET: (id: string) => TASK_SERVICE_ENDPOINTS.task_get.replace('{task_id}', id),
    /** 更新任务 */
    UPDATE: (id: string) => TASK_SERVICE_ENDPOINTS.task_update.replace('{task_id}', id),
    /** 删除任务 */
    DELETE: (id: string) => TASK_SERVICE_ENDPOINTS.task_delete.replace('{task_id}', id),
    /** 暂停任务（级联子任务） */
    PAUSE: (id: string) => TASK_SERVICE_ENDPOINTS.task_pause.replace('{task_id}', id),
    /** 恢复任务（级联子任务） */
    RESUME: (id: string) => TASK_SERVICE_ENDPOINTS.task_resume.replace('{task_id}', id),
    /** 取消任务 */
    CANCEL: (id: string) => TASK_SERVICE_ENDPOINTS.task_cancel.replace('{task_id}', id),
  },
  /** 长期任务相关 - task_service 插件端点（projects 域，对齐容器任务） */
  PROJECTS: {
    /** 获取长期任务列表 */
    LIST: TASK_SERVICE_ENDPOINTS.projects_list,
    /** 创建长期任务 */
    CREATE: TASK_SERVICE_ENDPOINTS.projects_create,
    /** 获取长期任务详情 */
    GET: (id: string) => TASK_SERVICE_ENDPOINTS.project_get.replace('{project_id}', id),
    /** 切换自动执行开关 */
    TOGGLE_AUTO_EXECUTE: (id: string) =>
      TASK_SERVICE_ENDPOINTS.project_toggle_auto_execute.replace('{project_id}', id),
    /** 暂停长期任务 */
    PAUSE: (id: string) => TASK_SERVICE_ENDPOINTS.project_pause.replace('{project_id}', id),
    /** 恢复长期任务 */
    RESUME: (id: string) => TASK_SERVICE_ENDPOINTS.project_resume.replace('{project_id}', id),
    /** 删除长期任务 */
    DELETE: (id: string) => TASK_SERVICE_ENDPOINTS.project_delete.replace('{project_id}', id),
  },
  /** 任务阶段相关 - task_service 插件端点 */
  TASK_PHASES: {
    /** 获取任务阶段状态 */
    GET_STATUS: (taskId: string) =>
      TASK_SERVICE_ENDPOINTS.task_phase_status.replace('{task_id}', taskId),
    /** 完成准备阶段 */
    COMPLETE_PREPARE: (taskId: string) =>
      TASK_SERVICE_ENDPOINTS.task_phase_complete_prepare.replace('{task_id}', taskId),
    /** 完成执行阶段 */
    COMPLETE_EXECUTE: (taskId: string) =>
      TASK_SERVICE_ENDPOINTS.task_phase_complete_execute.replace('{task_id}', taskId),
    /** 获取阶段产物 */
    GET_OUTPUT: (taskId: string, phase: string) =>
      TASK_SERVICE_ENDPOINTS.task_phase_output
        .replace('{task_id}', taskId)
        .replace('{phase}', phase),
  },
  /** 任务验收标准评估相关 - task_service 插件端点 */
  TASK_EVALUATION: {
    /** 获取任务所有验收标准 */
    LIST: (taskId: string) =>
      TASK_SERVICE_ENDPOINTS.task_ac_list.replace('{task_id}', taskId),
    /** 评估单个验收标准 */
    EVALUATE: (taskId: string, acId: string) =>
      TASK_SERVICE_ENDPOINTS.task_ac_evaluate
        .replace('{task_id}', taskId)
        .replace('{ac_id}', acId),
    /** 评估所有验收标准 */
    EVALUATE_ALL: (taskId: string) =>
      TASK_SERVICE_ENDPOINTS.task_ac_evaluate_all.replace('{task_id}', taskId),
    /** 获取验收标准评估结果 */
    GET_RESULT: (taskId: string, acId: string) =>
      TASK_SERVICE_ENDPOINTS.task_ac_result
        .replace('{task_id}', taskId)
        .replace('{ac_id}', acId),
  },
  /** 思考模式相关 - llm_service 插件端点（生成物投影，原 channel_api thinking-mode 域） */
  THINKING_MODE: {
    /** 获取所有支持思考模式的模型 */
    MODELS: LLM_SERVICE_ENDPOINTS.thinking_mode_models_list,
    /** 获取指定模型的思考模式信息 */
    MODEL_INFO: (modelName: string) =>
      LLM_SERVICE_ENDPOINTS.thinking_mode_model_info.replace('{model_name}', modelName),
    /** 切换思考模式 */
    SWITCH: LLM_SERVICE_ENDPOINTS.thinking_mode_switch,
    /** 获取思考模式推荐 */
    RECOMMENDATIONS: LLM_SERVICE_ENDPOINTS.thinking_mode_recommendations,
    /** 检查模型是否支持思考模式 */
    CHECK_SUPPORT: (modelName: string) =>
      LLM_SERVICE_ENDPOINTS.thinking_mode_check.replace('{model_name}', modelName),
    /** 思考模式服务健康检查 */
    HEALTH: LLM_SERVICE_ENDPOINTS.thinking_mode_health,
  },
  /** 成本控制相关 - cost_control 插件端点（生成物投影，已从内核 compat_routes 迁出） */
  COST_CONTROL: {
    /** 获取预算状态 */
    BUDGET_STATUS: COST_CONTROL_ENDPOINTS.cost_budget_status,
    /** 获取使用统计 */
    USAGE_STATISTICS: COST_CONTROL_ENDPOINTS.cost_usage_statistics,
    /** 获取成本配置 */
    CONFIG: COST_CONTROL_ENDPOINTS.cost_config,
    /** 获取成本报表 */
    REPORT: COST_CONTROL_ENDPOINTS.cost_report,
    /** 重置预算 */
    BUDGET_RESET: COST_CONTROL_ENDPOINTS.cost_budget_reset,
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
  /** 人类交互相关 - approval_service 插件端点（生成物投影，原 channel_api interaction 域） */
  INTERACTION: {
    SUBMIT_RESPONSE: APPROVAL_SERVICE_ENDPOINTS.interaction_response,
    APPROVE: (requestId: string) =>
      APPROVAL_SERVICE_ENDPOINTS.interaction_approve.replace('{request_id}', requestId),
    DENY: (requestId: string) =>
      APPROVAL_SERVICE_ENDPOINTS.interaction_deny.replace('{request_id}', requestId),
    CANCEL: (requestId: string) =>
      APPROVAL_SERVICE_ENDPOINTS.interaction_cancel.replace('{request_id}', requestId),
    VIEWED: (requestId: string) =>
      APPROVAL_SERVICE_ENDPOINTS.interaction_viewed.replace('{request_id}', requestId),
    PENDING: APPROVAL_SERVICE_ENDPOINTS.interaction_pending,
    GET: (requestId: string) =>
      APPROVAL_SERVICE_ENDPOINTS.interaction_get.replace('{request_id}', requestId),
  },
  /** 知识库相关 - hindsight_memory_service 插件端点（生成物投影，原 channel_api knowledge-base 域） */
  KNOWLEDGE_BASE: {
    /** 获取知识库列表 */
    LIST: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_list,
    /** 获取知识库统计 */
    STATS: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_stats,
    /** 上传文件 */
    UPLOAD: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_upload,
    /** 获取知识库详情 */
    GET: (id: string) =>
      HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_item_get.replace('{item_id}', id),
    /** 删除知识库 */
    DELETE: (id: string) =>
      HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_item_delete.replace('{item_id}', id),
    /** 检查知识库 */
    CHECK: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_check,
    /** 获取分类列表 */
    CATEGORIES: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_categories_list,
    /** 创建分类 */
    CREATE_CATEGORY: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_category_create,
    /** 删除分类 */
    DELETE_CATEGORY: (name: string) =>
      HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_category_delete.replace('{name}', name),
    /** 获取标签列表 */
    TAGS: HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_tags,
  },
  /** 全局搜索相关 - monitoring 插件端点（原 channel_api search 假数据，接真搜内核 messages/pipeline-state） */
  SEARCH: {
    /** 统一搜索（会话 + 消息），参数 q/type/limit */
    GLOBAL: MONITORING_ENDPOINTS.mon_search_global,
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
