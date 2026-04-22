/**
 * 认证 API 服务
 *
 * 暴露接口：
 * - login(username, password, options): 用户登录
 * - register(username, password, email, options): 用户注册
 * - refreshToken(token, options): 刷新访问令牌
 * - logout(refreshTokenValue, logoutAll, options): 用户登出
 * - getCurrentUser(options): 获取当前用户信息
 */

import apiClient from './client'
import {
  API_ENDPOINTS,
} from '../../constants/api'
import { requestWithRetry } from '../../utils/retry'
import type { RetryOptions } from '../../utils/retry'
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
  options: RetryOptions = {}
): Promise<LoginResponse> {
  // 参数验证
  validateUsername(username)
  validatePassword(password)

  // 构造请求数据
  const requestData: LoginRequest = {
    username: username.trim(),
    password,
  }

  // 发送请求（带重试）
  return requestWithRetry(async () => {
    const response = await apiClient.post<LoginResponse>(
      API_ENDPOINTS.AUTH.LOGIN,
      requestData
    )
    return response.data
  }, options)
}

export async function register(
  username: string,
  password: string,
  email: string,
  options: RetryOptions = {}
): Promise<RegisterResponse> {
  // 参数验证
  validateUsername(username)
  validatePassword(password)
  validateEmail(email)

  // 构造请求数据
  const requestData: RegisterRequest = {
    username: username.trim(),
    password,
    email: email.trim(),
  }

  // 发送请求（带重试）
  return requestWithRetry(async () => {
    const response = await apiClient.post<RegisterResponse>(
      API_ENDPOINTS.AUTH.REGISTER,
      requestData
    )
    return response.data
  }, options)
}

export async function refreshToken(
  token: string,
  options: RetryOptions = {}
): Promise<RefreshResponse> {
  // 参数验证
  validateRefreshToken(token)

  // 构造请求数据（与后端RefreshRequest对齐）
  const requestData: RefreshRequest = {
    refresh_token: token,
  }

  // 发送请求（带重试）
  return requestWithRetry(async () => {
    const response = await apiClient.post<RefreshResponse>(
      API_ENDPOINTS.AUTH.REFRESH_TOKEN,
      requestData
    )
    return response.data
  }, options)
}

export async function logout(
  refreshTokenValue?: string,
  logoutAll: boolean = false,
  options: RetryOptions = {}
): Promise<LogoutResponse> {
  // 构造请求数据
  const requestData: LogoutRequest = {
    refresh_token: refreshTokenValue,
    logout_all: logoutAll,
  }

  // 发送请求（带重试）
  return requestWithRetry(async () => {
    const response = await apiClient.post<LogoutResponse>(
      API_ENDPOINTS.AUTH.LOGOUT,
      requestData
    )
    return response.data
  }, options)
}

export async function getCurrentUser(
  options: RetryOptions = {}
): Promise<UserInfoResponse> {
  // 发送请求（带重试）
  return requestWithRetry(async () => {
    const response = await apiClient.get<UserInfoResponse>(
      API_ENDPOINTS.AUTH.ME
    )
    return response.data
  }, options)
}
