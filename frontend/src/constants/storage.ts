/**
 * 本地存储键名常量
 *
 * 统一管理所有 localStorage 键名，避免硬编码字符串
 * Requirements: 2.2
 */

/**
 * 本地存储键名常量
 */
export const STORAGE_KEYS = {
  /** 访问令牌 */
  ACCESS_TOKEN: 'access_token',
  /** 刷新令牌 */
  REFRESH_TOKEN: 'refresh_token',
  /** 访问令牌过期时间（时间戳） */
  ACCESS_TOKEN_EXPIRY: 'access_token_expiry',
  /** 认证用户信息 */
  AUTH_USER: 'auth_user',
  /** 用户信息 */
  USER_INFO: 'user_info',
  /** 简化版主题设置 */
  THEME: 'theme',
  /** 主题选择（配置文件名） */
  THEME_SELECTION: 'selected_theme_name',
  /** 完整主题配置 */
  THEME_CONFIG: 'app_theme_config',
  /** 自定义背景配置 */
  CUSTOM_BACKGROUND: 'custom_background',
  /** 侧边栏折叠状态 */
  SIDEBAR_COLLAPSED: 'sidebar_collapsed',
  /** 最后活跃会话ID */
  LAST_ACTIVE_SESSION: 'last_active_session',
  /** Agent 偏好设置 - Requirements: 13.2, 13.4, 13.5 */
  AGENT_PREFERENCES: 'agent_preferences',
  /** 任务状态面板折叠状态 */
  TASK_PANEL_COLLAPSED: 'task_panel_collapsed',
  /** 工作区面板折叠状态 */
  WORKSPACE_COLLAPSED: 'workspace_collapsed',
  /** 思考模式启用状态 */
  THINKING_MODE_ENABLED: 'thinking_mode_enabled',
} as const

/**
 * 存储键名类型
 */
export type StorageKey = (typeof STORAGE_KEYS)[keyof typeof STORAGE_KEYS]

/**
 * Agent 偏好设置接口
 * Requirements: 13.2, 13.4, 13.5
 */
export interface AgentPreferences {
  /** 默认 Agent ID */
  defaultAgentId: string | null
  /** 常用 Agent ID 列表（最多 10 个） */
  favoriteAgentIds: string[]
}

/** 常用 Agent 最大数量 - Requirements: 13.5 */
export const MAX_FAVORITE_AGENTS = 10
