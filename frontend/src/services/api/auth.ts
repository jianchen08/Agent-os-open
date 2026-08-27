/** 认证 API 服务 */

import apiClient from './client'
import { API_ENDPOINTS } from '../../constants/api'
import { requestWithRetry } from '../../utils/retry'
import type {
  LoginResponse,
  RegisterResponse,
  RefreshResponse,
  LogoutResponse,
  UserInfoResponse,
  LoginRequest,
  RegisterRequest,
  RefreshRequest,
  LogoutRequest,
} from '../../types/api'
import type { RetryOptions } from '../../utils/retry'

class ValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ValidationError'
  }
}

function validateUsername(username: string): void {
  if (!username || username.trim().length === 0) {
    throw new ValidationError('用户名不能为空')
  }
  if (username.length < 3) {
    throw new ValidationError('用户名长度至少为3个字符')
  }
}

function validatePassword(password: string): void {
  if (!password || password.trim().length === 0) {
    throw new ValidationError('密码不能为空')
  }
  if (password.length < 8) {
    throw new ValidationError('密码长度至少为8个字符')
  }
}

function validateEmail(email: string): void {
  const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/
  if (!emailRegex.test(email)) {
    throw new ValidationError('邮箱格式不正确')
  }
}

function validateRefreshToken(token: string): void {
  if (!token || token.trim().length === 0) {
    throw new ValidationError('Refresh token不能为空')
  }
  if (token.length < 10) {
    throw new ValidationError('Refresh token格式不正确')
  }
}

export async function login(
  username: string,
  password: string,
  options: RetryOptions = {},
): Promise<LoginResponse> {
  validateUsername(username)
  validatePassword(password)

  const requestData: LoginRequest = {
    username: username.trim(),
    password,
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<LoginResponse>(API_ENDPOINTS.AUTH.LOGIN, requestData)
    return response.data
  }, options)
}

export async function register(
  username: string,
  password: string,
  email: string,
  options: RetryOptions = {},
): Promise<RegisterResponse> {
  validateUsername(username)
  validatePassword(password)
  validateEmail(email)

  const requestData: RegisterRequest = {
    username: username.trim(),
    password,
    email: email.trim(),
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<RegisterResponse>(
      API_ENDPOINTS.AUTH.REGISTER,
      requestData,
    )
    return response.data
  }, options)
}

export async function refreshToken(
  token: string,
  options: RetryOptions = {},
): Promise<RefreshResponse> {
  validateRefreshToken(token)

  const requestData: RefreshRequest = {
    refresh_token: token,
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<RefreshResponse>(
      API_ENDPOINTS.AUTH.REFRESH_TOKEN,
      requestData,
      {
        // Authorization 显式置空串：client.ts 请求拦截器对非空值一律注入
        // Bearer <access_token>，会把 access token 带进 refresh 请求 → 后端
        // 判定「期望 refresh 类型」直接 401。空串被拦截器识别为「不带认证」，
        // refresh_token 只经 body 传递。
        headers: { Authorization: '' },
      },
    )
    return response.data
  }, options)
}

export async function logout(
  refreshTokenValue?: string,
  logoutAll: boolean = false,
  options: RetryOptions = {},
): Promise<LogoutResponse> {
  const requestData: LogoutRequest = {
    refresh_token: refreshTokenValue,
    logout_all: logoutAll,
  }

  return requestWithRetry(async () => {
    const response = await apiClient.post<LogoutResponse>(API_ENDPOINTS.AUTH.LOGOUT, requestData)
    return response.data
  }, options)
}

export async function getCurrentUser(options: RetryOptions = {}): Promise<UserInfoResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<UserInfoResponse>(API_ENDPOINTS.AUTH.ME)
    return response.data
  }, options)
}
