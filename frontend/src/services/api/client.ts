/**
 * API客户端配置
 *
 * 创建axios实例并配置请求/响应拦截器
 * 集成错误处理、令牌刷新和重试机制
 *
 * Requirements: 2.2, 2.3, 2.4
 */

import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { API_BASE_URL, API_TIMEOUT, API_ENDPOINTS } from '../../constants/api'
import { STORAGE_KEYS } from '../../constants/storage'
import { isRetryableError } from '../../utils/retry'
import { triggerAuthExpired } from '../authCallbacks'
import { reportError, ErrorType, ErrorSeverity } from '../errorReporting'
import type { ApiError, RefreshResponse } from '../../types/api'

/**
 * 令牌刷新状态管理
 * 防止多个请求同时刷新令牌
 *
 * BUG-FIX-fix_20260622_refresh_misclassify_logout:
 * 订阅回调签名改为 (token | null)，null 表示刷新失败。
 * 刷新失败时也通知订阅者，避免等待的请求 Promise 永不 resolve 导致卡死。
 * （原代码因"任何失败都 logout→整页刷新"掩盖了此问题，改不 logout 后需显式处理。）
 */
let isRefreshing = false
let refreshSubscribers: Array<(token: string | null) => void> = []

/**
 * 订阅令牌刷新完成事件
 * @param callback 刷新成功传新 token，失败传 null
 */
function subscribeTokenRefresh(callback: (token: string | null) => void): void {
  refreshSubscribers.push(callback)
}

/**
 * 通知所有订阅者令牌已刷新（成功）
 */
function onTokenRefreshed(token: string): void {
  refreshSubscribers.forEach((callback) => callback(token))
  refreshSubscribers = []
}

/**
 * 通知所有订阅者令牌刷新失败
 *
 * BUG-FIX-fix_20260622_refresh_misclassify_logout:
 * 让等待中的请求收到失败信号并 reject，避免 Promise 永不 resolve。
 */
function notifyRefreshFailed(): void {
  refreshSubscribers.forEach((callback) => callback(null))
  refreshSubscribers = []
}

/**
 * 清除认证信息并重定向到登录页
 *
 * BUG-FIX-fix_20260506_001: 增加停止自生长闭环轮询
 * 问题根因: clearAuthAndRedirect 未停止轮询导致 401 死循环
 * 修复方案: 在清除认证前先销毁 GrowthLoop 停止轮询
 *
 * Requirements: 2.4
 *
 * BUG-FIX-fix_20260622_workspace_state_loss:
 * 问题根因: 此函数仅清理认证相关 key，不应触碰任何工作区状态
 *          （LAST_ACTIVE_SESSION / pipeline-messages / agent-tabs 等）。
 *          认证失效≠工作区状态失效，重登后需恢复原视图。
 * 修复方案: 显式约束只清 4 个认证 key，禁止在此扩展清理工作区状态。
 */
async function clearAuthAndRedirect(): Promise<void> {
  // BUG-FIX-fix_20260507_002: await 销毁自生长闭环再清理认证
  // 问题根因: import().then() 异步销毁，后续 401 请求可能在销毁前发出
  // 修复方案: 使用 await import() 确保销毁完成后再清理
  try {
    const { destroyGrowthLoop } = await import('../modules/GrowthLoop')
    destroyGrowthLoop()
  } catch {
    // 模块未加载过，忽略
  }

  // 仅清除认证相关的 4 个 key，禁止清理任何工作区状态
  // （LAST_ACTIVE_SESSION / pipeline-messages / agent-tabs / layout-mode 等保留，供重登后恢复）
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN)
  localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
  localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)

  // 通过回调机制通知 store 清除认证状态
  triggerAuthExpired()

  // 报告认证错误
  reportError('认证已过期，请重新登录', ErrorType.AUTHENTICATION, ErrorSeverity.WARNING, {
    code: '401',
  })

  // 重定向到登录页（如果不在登录页）
  // 注意：window.location.href 是整页刷新，会丢失内存中的 zustand 状态。
  // 此处仅在「真正认证失效」时才到达，故整页刷新可接受。
  if (typeof window !== 'undefined' && !window.location.pathname.includes('/login')) {
    window.location.href = '/login'
  }
}

/**
 * 判断 token 刷新错误是否为「真正认证失效」
 *
 * BUG-FIX-fix_20260622_refresh_misclassify_logout:
 * 问题根因: 原刷新逻辑把任何刷新失败（含网络断开/超时/CORS/5xx）一律视为
 *          认证过期并强制 logout，导致网络抖动期间用户被误踢出。
 * 修复方案: 只有当刷新请求被后端明确拒绝（HTTP 401/403）时，才视为认证失效；
 *          其他情况（无 response 的网络错误、超时、5xx 服务端错误）视为暂时性故障，
 *          不登出，保留旧 token 让上层 retry/后续请求继续尝试。
 * 影响范围: client.ts 401 拦截器、authStore.refreshToken、initializeAuth
 */
function isDefinitelyAuthFailure(error: unknown): boolean {
  // axios 错误对象：有 response 且状态码明确为 401/403 → 真认证失效
  const status = (error as AxiosError)?.response?.status
  if (status === 401 || status === 403) {
    return true
  }
  // 其余情况（无 response 的网络错误、超时 ERR_NETWORK/ETIMEDOUT、5xx）→ 暂时性故障
  return false
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
    // 直接从 localStorage 获取 access_token
    const token = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)

    // 如果token存在，添加到请求头
    if (token && config.headers) {
      config.headers.Authorization = `Bearer ${token}`
    }

    return config
  },
  (error: AxiosError) => {
    // 请求错误处理
    return Promise.reject(error)
  },
)

/**
 * 响应拦截器
 * 处理响应错误、token刷新和自动重试
 *
 * Requirements: 2.2, 2.3, 2.4
 */
apiClient.interceptors.response.use(
  (response) => {
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
        // BUG-FIX-fix_20260622_refresh_misclassify_logout:
        // 问题根因: 原逻辑对刷新请求的任何错误都立即 logout，但刷新请求也可能因
        //          网络断开/超时/5xx 失败，此时并非认证失效，误登出会丢失用户工作上下文。
        // 修复方案: 仅当后端明确返回 401/403（真认证失效）才 logout；
        //          网络错误/超时/5xx 视为暂时性故障，reject 让上层重试，不登出。
        if (isDefinitelyAuthFailure(error)) {
          // refresh_token 真正失效，这是正常的认证过期场景
          // 静默处理，不报告错误，直接清除认证状态并重定向
          await clearAuthAndRedirect()
        }
        return Promise.reject(error)
      }

      // 如果正在刷新令牌，等待刷新完成
      if (isRefreshing) {
        return new Promise((resolve, reject) => {
          subscribeTokenRefresh((token: string | null) => {
            // BUG-FIX-fix_20260622_refresh_misclassify_logout:
            // token 为 null 表示刷新失败，reject 让上层处理（重试或报错），避免 Promise 永挂。
            if (!token) {
              reject(error)
              return
            }
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
        const storedRefreshToken = localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)

        if (storedRefreshToken) {
          // 使用与后端一致的请求格式
          const response = await axios.post<RefreshResponse>(
            `${API_BASE_URL}${API_ENDPOINTS.AUTH.REFRESH_TOKEN}`,
            { refresh_token: storedRefreshToken },
            {
              headers: {
                'Content-Type': 'application/json',
              },
            },
          )

          const { access_token, refresh_token } = response.data

          // 保存新 token 到 localStorage
          // Requirements: 2.2
          localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, access_token)
          if (refresh_token) {
            localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refresh_token)
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
          await clearAuthAndRedirect()
          return Promise.reject(error)
        }
      } catch (refreshError) {
        // BUG-FIX-fix_20260622_refresh_misclassify_logout:
        // 问题根因: 原逻辑对刷新请求的任何错误都立即 logout，但刷新请求也可能因
        //          网络断开/超时/5xx 失败，此时并非认证失效，误登出会丢失用户工作上下文。
        // 修复方案: 仅当后端明确返回 401/403（真认证失效）才 logout；
        //          网络错误/超时/5xx 视为暂时性故障，reject 让上层重试，保留旧 token。
        isRefreshing = false

        if (isDefinitelyAuthFailure(refreshError)) {
          // refresh_token 真正失效（后端明确拒绝），清除认证信息并重定向
          notifyRefreshFailed()
          await clearAuthAndRedirect()
        } else {
          // 网络错误/超时/5xx：暂性故障，不登出，通知等待的请求失败（让其 reject 重试）
          // 保留旧 token，让后续用户操作或 retry 机制继续尝试
          notifyRefreshFailed()
          reportError(
            '网络异常，认证刷新暂时失败，请检查网络后重试',
            ErrorType.NETWORK,
            ErrorSeverity.WARNING,
            { showToast: false },
          )
        }
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
    const shouldRetry = isRetryableError(error) && originalRequest._retryCount < 2

    if (shouldRetry) {
      originalRequest._retryCount++

      // 计算延迟时间（指数退避）
      const delayTime = Math.min(1000 * Math.pow(2, originalRequest._retryCount - 1), 5000)

      // 报告重试信息（不显示Toast，只记录到控制台）
      reportError(
        `请求失败，${delayTime}ms 后进行第 ${originalRequest._retryCount} 次重试`,
        ErrorType.NETWORK,
        ErrorSeverity.INFO,
        {
          showToast: false,
          code: apiError.code,
        },
      )

      // 等待后重试
      await new Promise((resolve) => setTimeout(resolve, delayTime))
      return apiClient(originalRequest)
    }

    // 判断是否应该静默处理某些 404 错误
    // 这些错误通常发生在：
    // 1. 消息刚创建，数据库还未保存完成
    // 2. 消息已被删除
    // 3. 临时消息 ID 更新后，数据库还未更新
    // 4. 子管道消息尚不存在（子 Agent 未开始执行）
    const requestUrl = error.config?.url || ''
    const shouldSilentIgnore =
      error.response?.status === 404 &&
      (errorMessage.includes('消息不存在') ||
        errorMessage.includes('[VALIDATION] 消息不存在') ||
        errorMessage.includes('不存在') ||
        requestUrl.includes('/messages'))

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

    const isOptionalEndpoint = requestUrl.includes('/files/capabilities')

    if (!isOptionalEndpoint) {
      reportError(
        apiError.message,
        errorType,
        errorType === ErrorType.AUTHENTICATION ? ErrorSeverity.WARNING : ErrorSeverity.ERROR,
        {
          code: apiError.code,
          details: apiError.details,
        },
      )
    }

    return Promise.reject(apiError)
  },
)

export default apiClient

// 同时导出默认导出和命名导出
export { apiClient }
