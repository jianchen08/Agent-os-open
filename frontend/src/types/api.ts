/**
 * API请求和响应类型定义
 *
 * 与后端API响应格式对齐
 */

/**
 * 线程（会话）类型
 */
export interface Thread {
  thread_id: string
  current_state: string
  intent: string | null
  created_at: string
  updated_at: string
  message_count?: number
  status?: string
  metadata?: Record<string, unknown>
  agent_id?: string | null
}

/**
 * 登录响应（与后端LoginResponse对齐）
 */
export interface LoginResponse {
  /** 访问令牌 */
  access_token: string
  /** 刷新令牌 */
  refresh_token: string
  /** 令牌类型 */
  token_type: string
  /** 访问令牌过期时间（秒） */
  expires_in: number
  /** 首登强制改密标记（播种账号为 true，前端拦截强制改密后放行） */
  must_change_password?: boolean
}

/**
 * 注册响应（与后端TokenResponse对齐）
 * 注册成功后自动登录，返回token
 */
export interface RegisterResponse {
  /** 访问令牌 */
  access_token: string
  /** 刷新令牌 */
  refresh_token: string
  /** 令牌类型 */
  token_type: string
  /** 访问令牌过期时间（秒） */
  expires_in: number
  /** 首登强制改密标记（注册用户恒为 false） */
  must_change_password?: boolean
}

/**
 * 令牌刷新响应（与后端RefreshResponse对齐）
 */
export interface RefreshResponse {
  /** 新的访问令牌 */
  access_token: string
  /** 新的刷新令牌（D12-7 单次轮换恒返回） */
  refresh_token?: string
  /** 令牌类型 */
  token_type: string
  /** 访问令牌过期时间（秒） */
  expires_in: number
  /** 首登强制改密标记（随轮换透出，恢复会话时继续拦截） */
  must_change_password?: boolean
}

/**
 * 改密请求（与后端ChangePasswordRequest对齐）
 */
export interface ChangePasswordRequest {
  /** 旧口令 */
  old_password: string
  /** 新口令 */
  new_password: string
}

/**
 * 登出响应（与后端LogoutResponse对齐）
 */
export interface LogoutResponse {
  /** 是否成功 */
  success: boolean
  /** 响应消息 */
  message: string
}

/**
 * 用户信息响应（与后端UserResponse对齐）
 */
export interface UserInfoResponse {
  /** 用户ID */
  id: string
  /** 用户名 */
  username: string
  /** 邮箱 */
  email: string
  /** 用户角色 */
  role: 'admin' | 'user' | 'guest'
  /** 是否激活 */
  is_active: boolean
  /** 创建时间 */
  created_at: string
  /** 最后登录时间 */
  last_login_at?: string
  /** 首登强制改密标记（播种账号为 true，改密成功后清除） */
  must_change_password?: boolean
}

/**
 * 登录请求（与后端LoginRequest对齐）
 */
export interface LoginRequest {
  /** 用户名 */
  username: string
  /** 密码 */
  password: string
}

/**
 * 注册请求（与后端RegisterRequest对齐）
 */
export interface RegisterRequest {
  /** 用户名 */
  username: string
  /** 密码 */
  password: string
  /** 邮箱 */
  email: string
}

/**
 * 令牌刷新请求（与后端RefreshRequest对齐）
 */
export interface RefreshRequest {
  /** 刷新令牌 */
  refresh_token: string
}

/**
 * 登出请求（与后端LogoutRequest对齐）
 */
export interface LogoutRequest {
  /** 刷新令牌（可选） */
  refresh_token?: string
  /** 是否登出所有设备 */
  logout_all?: boolean
}

/**
 * 错误来源（与 config/kernel/error_codes.json sources.enum 一致，单一真值源）
 */
export type ErrorSource = 'kernel' | 'plugin' | 'llm' | 'infra' | 'frontend'

/**
 * 统一错误信封（REST 与 WS 同构，单一真值源 config/kernel/error_codes.json）：
 * code 为稳定机器码（非 HTTP 状态码），source 供前端渲染来源标签，
 * retryable 驱动重试按钮，details/request_id 预留（P2 贯通）。
 */
export interface ErrorEnvelope {
  /** 稳定机器码（如 RESOURCE_NOT_FOUND / ENGINE_RUN_FAILED） */
  code: string
  /** 人可读文案（后端原文透传不脱敏） */
  message: string
  /** 错误来源（内核/插件/LLM/基础设施/前端） */
  source?: ErrorSource
  /** 是否可重试 */
  retryable?: boolean
  /** 结构化上下文（可选） */
  details?: unknown
  /** 日志关联 ID（预留，当前恒 null） */
  request_id?: string | null
}

/**
 * API错误响应
 */
export interface ApiError extends ErrorEnvelope {
  /** 错误代码（兼容旧形态：HTTP 状态码字符串或 axios 错误码） */
  code: string
  /** 错误消息 */
  message: string
  /** 错误详情（可选；任意结构，按需在消费端窄化） */
  details?: unknown
}

