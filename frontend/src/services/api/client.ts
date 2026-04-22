/**
 * API客户端配置
 *
 * 创建axios实例并配置请求/响应拦截器
 * 集成错误处理、令牌刷新和重试机制
 *
 * Requirements: 2.2, 2.3, 2.4
 */

import axios, { AxiosError } from 'axios'
import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios'
import { API_BASE_URL, API_TIMEOUT, API_ENDPOINTS } from '../../constants/api'
import { STORAGE_KEYS } from '../../constants/storage'
import type { ApiError, RefreshResponse } from '../../types/api'
import { reportError, ErrorType, ErrorSeverity } from '../errorReporting'
import { isRetryableError } from '../../utils/retry'
import { tokenManager } from '../../stores/tokenManager'
import { useAuthStore } from '../../stores/authStore'

/**
 * 令牌刷新状态管理
 * 防止多个请求同时刷新令牌
 */
let isRefreshing = false
let refreshSubscribers: Array<(token: string) => void> = []

/**
 * 订阅令牌刷新完成事件
 */
function subscribeTokenRefresh(callback: (token: string) => void): void {
  refreshSubscribers.push(callback)
}

/**
 * 通知所有订阅者令牌已刷新
 */
function onTokenRefreshed(token: string): void {
  refreshSubscribers.forEach(callback => callback(token))
  refreshSubscribers = []
}

/**
 * 清除认证信息并重定向到登录页
 *
 * Requirements: 2.4
 */
function clearAuthAndRedirect(): void {
  // 使用 tokenManager 清除令牌
  tokenManager.clearAllTokens()

  // 同步清除 authStore 状态（避免状态不一致）
  // 注意：这里不调用 logout() 以避免循环调用 API
  // 清除 localStorage 中的其他认证相关数据
  localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)

  // 直接重置 store 状态
  useAuthStore.setState({
    user: null,
    token: null,
    refreshTokenValue: null,
    isAuthenticated: false,
    error: null,
  })

  // 报告认证错误
  reportError(
    '认证已过期，请重新登录',
    ErrorType.AUTHENTICATION,
    ErrorSeverity.WARNING,
    {
      code: '401',
    }
  )

  // 重定向到登录页（如果不在登录页）
  if (
    typeof window !== 'undefined' &&
    !window.location.pathname.includes('/login')
  ) {
    window.location.href = '/login'
  }
}

/**
 * 创建axios实例
 */
const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: API_TIMEOUT,
  headers: {
    'Content-Type': 'application/json',
  },
})

/**
 * 请求拦截器
 * 在请求发送前添加认证token
 */
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // 使用 tokenManager 获取 access_token
    const token = tokenManager.getToken()

    // 如果token存在，添加到请求头
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }

    return config
  },
  (error: AxiosError) => {
    // 请求错误处理
    return Promise.reject(error)
  }
)

/**
 * 响应拦截器
 * 处理响应错误、token刷新和自动重试
 *
 * Requirements: 2.2, 2.3, 2.4
 */
apiClient.interceptors.response.use(
  response => {
    // 成功响应直接返回
    return response
  },
  async (error: AxiosError) => {
    const originalRequest = error.config as
      | (InternalAxiosRequestConfig & {
          _retry?: boolean
          _retryCount?: number
        })
      | undefined

    // 如果没有原始请求配置，直接拒绝
    if (!originalRequest) {
      return Promise.reject(error)
    }

    // 初始化重试计数
    if (originalRequest._retryCount === undefined) {
      originalRequest._retryCount = 0
    }

    // 如果是401错误且未重试过，尝试刷新token
    // Requirements: 2.3
    if (error.response?.status === 401 && !originalRequest._retry) {
      // BUG-FIX-fix_20260401_143000_token_revoked
      // 问题根因: 当 refresh_token 失效时，前端会报告错误到控制台，但这实际上是正常的认证过期场景
      // 修复方案: 对 /auth/refresh 端点的 401 错误进行特殊处理，静默处理而不报告错误
      // 影响范围: 前端认证模块、用户体验
      // 修复日期: 2026-04-01
      
      // 检查是否是 refresh_token 刷新失败
      const isRefreshTokenRequest = originalRequest.url?.includes('/auth/refresh')
      
      if (isRefreshTokenRequest) {
        // refresh_token 已失效，这是正常的认证过期场景
        // 静默处理，不报告错误，直接清除认证状态并重定向
        clearAuthAndRedirect()
        return Promise.reject(error)
      }
      
      // 如果正在刷新令牌，等待刷新完成
      if (isRefreshing) {
        return new Promise(resolve => {
          subscribeTokenRefresh((token: string) => {
            if (originalRequest.headers) {
              originalRequest.headers.Authorization = `Bearer ${token}`
            }
            resolve(apiClient(originalRequest))
          })
        })
      }

      originalRequest._retry = true
      isRefreshing = true

      try {
        // 尝试刷新token
        const storedRefreshToken = tokenManager.getRefreshToken()

        if (storedRefreshToken) {
          // 使用与后端一致的请求格式
          const response = await axios.post<RefreshResponse>(
            `${API_BASE_URL}${API_ENDPOINTS.AUTH.REFRESH_TOKEN}`,
            { refresh_token: storedRefreshToken },
            {
              headers: {
                'Content-Type': 'application/json',
              },
            }
          )

          const { access_token, refresh_token } = response.data

          // 使用 tokenManager 保存新token
          // Requirements: 2.2
          tokenManager.setToken(access_token)
          if (refresh_token) {
            tokenManager.setRefreshToken(refresh_token)
          }

          // 通知所有等待的请求
          onTokenRefreshed(access_token)
          isRefreshing = false

          // 更新原请求的token
          if (originalRequest.headers) {
            originalRequest.headers.Authorization = `Bearer ${access_token}`
          }

          // 重试原请求
          return apiClient(originalRequest)
        } else {
          // 没有refresh_token，清除认证信息并重定向
          // Requirements: 2.4
          isRefreshing = false
          clearAuthAndRedirect()
          return Promise.reject(error)
        }
      } catch (refreshError) {
        // token刷新失败，清除认证信息并重定向
        // Requirements: 2.4
        isRefreshing = false
        refreshSubscribers = []
        clearAuthAndRedirect()
        return Promise.reject(refreshError)
      }
    }

    // 构建API错误对象
    // 处理 detail 可能是对象的情况（后端返回结构化错误）
    const responseData = error.response?.data as any
    let errorMessage: string

    if (typeof responseData === 'string') {
      // 处理 detail 是纯字符串的情况（例如："error.model_dump(...) - message"）
      // 提取真实错误消息（在最后一个 " - " 之后）
      const lastDashIndex = responseData.lastIndexOf(' - ')
      if (lastDashIndex !== -1) {
        errorMessage = responseData.substring(lastDashIndex + 3).trim()
      } else {
        errorMessage = responseData
      }
    } else if (typeof responseData?.message === 'string') {
      errorMessage = responseData.message
    } else if (typeof responseData?.detail === 'string') {
      errorMessage = responseData.detail
    } else if (typeof responseData?.detail?.message === 'string') {
      // 处理 detail 是对象且包含 message 字段的情况
      errorMessage = responseData.detail.message
    } else if (error.message) {
      errorMessage = error.message
    } else {
      errorMessage = '请求失败'
    }

    const apiError: ApiError = {
      code: error.response?.status?.toString() || error.code || 'UNKNOWN_ERROR',
      message: errorMessage,
      details: error.response?.data,
    }

    // 判断是否应该自动重试
    const shouldRetry =
      isRetryableError(error) && originalRequest._retryCount < 2

    if (shouldRetry) {
      originalRequest._retryCount++

      // 计算延迟时间（指数退避）
      const delayTime = Math.min(
        1000 * Math.pow(2, originalRequest._retryCount - 1),
        5000
      )

      // 报告重试信息（不显示Toast，只记录到控制台）
      reportError(
        `请求失败，${delayTime}ms 后进行第 ${originalRequest._retryCount} 次重试`,
        ErrorType.NETWORK,
        ErrorSeverity.INFO,
        {
          showToast: false,
          code: apiError.code,
        }
      )

      // 等待后重试
      await new Promise(resolve => setTimeout(resolve, delayTime))
      return apiClient(originalRequest)
    }

    // ✅ 判断是否应该静默处理某些 404 错误
    // 这些错误通常发生在：
    // 1. 消息刚创建，数据库还未保存完成
    // 2. 消息已被删除
    // 3. 临时消息 ID 更新后，数据库还未更新
    const shouldSilentIgnore =
      error.response?.status === 404 &&
      (errorMessage.includes('消息不存在') ||
        errorMessage.includes('[VALIDATION] 消息不存在') ||
        errorMessage.includes('不存在'))

    if (shouldSilentIgnore) {
      // 静默处理，不上报错误
      return Promise.reject(apiError)
    }

    // 不重试或重试次数已用完，报告错误
    const errorType =
      error.response?.status === 401 || error.response?.status === 403
        ? ErrorType.AUTHENTICATION
        : error.response?.status && error.response.status >= 500
          ? ErrorType.SERVER
          : error.response?.status && error.response.status >= 400
            ? ErrorType.VALIDATION
            : ErrorType.NETWORK

    reportError(
      apiError.message,
      errorType,
      errorType === ErrorType.AUTHENTICATION
        ? ErrorSeverity.WARNING
        : ErrorSeverity.ERROR,
      {
        code: apiError.code,
        details: apiError.details,
      }
    )

    return Promise.reject(apiError)
  }
)

export default apiClient

// 同时导出默认导出和命名导出
export { apiClient }
