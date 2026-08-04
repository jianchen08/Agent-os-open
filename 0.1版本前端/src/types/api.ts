/**
 * API请求和响应类型定义
 *
 * 与后端API响应格式对齐
 * Requirements: 1.1, 2.1, 2.5, 2.6
 */

import type { GraphData } from './graph'
import type { User, Session, Message } from './models'

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
}

/**
 * 令牌刷新响应（与后端RefreshResponse对齐）
 */
export interface RefreshResponse {
  /** 新的访问令牌 */
  access_token: string
  /** 新的刷新令牌（轮换时返回） */
  refresh_token?: string
  /** 令牌类型 */
  token_type: string
  /** 访问令牌过期时间（秒） */
  expires_in: number
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
}

/**
 * 认证响应
 */
export interface AuthResponse {
  /** 用户信息 */
  user: User
  /** 访问令牌 */
  token: string
  /** 刷新令牌（可选） */
  refreshToken?: string
}

/**
 * 令牌响应
 */
export interface TokenResponse {
  /** 访问令牌 */
  token: string
  /** 刷新令牌（可选） */
  refreshToken?: string
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
 * 发送消息请求
 */
export interface SendMessageRequest {
  /** 会话ID */
  sessionId: string
  /** 消息内容 */
  content: string
}

/**
 * 创建会话响应
 */
export interface CreateSessionResponse {
  /** 会话信息 */
  session: Session
}

/**
 * 获取会话列表响应
 */
export interface GetSessionsResponse {
  /** 会话列表 */
  sessions: Session[]
}

/**
 * 获取消息列表响应
 */
export interface GetMessagesResponse {
  /** 消息列表 */
  messages: Message[]
}

/**
 * 发送消息响应
 */
export interface SendMessageResponse {
  /** 消息信息 */
  message: Message
}

/**
 * 获取执行图响应
 */
export interface GetGraphResponse {
  /** 执行图数据 */
  graph: GraphData
}

/**
 * API错误响应
 */
export interface ApiError {
  /** 错误代码 */
  code: string
  /** 错误消息 */
  message: string
  /** 错误详情（可选） */
  details?: Record<string, unknown>
}

/**
 * 通用API响应包装
 */
export interface ApiResponse<T = unknown> {
  /** 是否成功 */
  success: boolean
  /** 响应数据 */
  data?: T
  /** 错误信息 */
  error?: ApiError
}

/**
 * 用户设置响应（与后端 UserSettingsResponse 对齐）
 * Requirements: 6.4, 6.5
 */
export interface UserSettingsResponse {
  /** 默认 Agent ID */
  default_agent_id: string | null
  /** 用户偏好设置 */
  preferences: Record<string, unknown>
}

/**
 * 用户设置更新请求（与后端 UserSettingsUpdateRequest 对齐）
 * Requirements: 6.4, 6.5
 */
export interface UserSettingsUpdateRequest {
  /** 默认 Agent ID */
  default_agent_id?: string | null
  /** 用户偏好设置 */
  preferences?: Record<string, unknown>
}

// ============================================================================
// 任务执行闭环 API 类型
// ============================================================================

// 以下类型从 task.ts 重新导出，避免重复定义
export type {
  CreateProjectRequest,
  GetProjectsResponse,
  GetProjectResponse,
  ToggleAutoExecuteRequest,
  ToggleAutoExecuteResponse,
  PauseProjectResponse,
  ResumeProjectResponse,
  GetTaskPhaseResponse,
  CompletePreparePhaseRequest,
  CompleteExecutePhaseRequest,
  GetPhaseOutputResponse,
  GetTaskACsResponse,
  EvaluateACRequest,
  EvaluateACResponse,
  GetACResultResponse,
} from './task'
