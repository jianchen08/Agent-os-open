/** API客户端配置 创建axios实例并配置请求/响应拦截器 */

import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { API_BASE_URL, API_TIMEOUT } from '../../constants/api'
import { STORAGE_KEYS } from '../../constants/storage'
import { isRetryableError } from '../../utils/retry'
import { triggerAuthExpired } from '../authCallbacks'
import { reportError, ErrorType, ErrorSeverity } from '../errorReporting'
import type { ApiError } from '../../types/api'

// NOTE: useAuthStore 通过运行时动态 import 引入，避免与 authStore.ts → auth.ts → client.ts
// 构成静态循环依赖（vitest/vite 在 transform 阶段解析静态 import 会失败）。
// 互斥锁仍由 authStore.refreshToken 的模块级 refreshInFlight 提供，所有调用方共享。

/** 清除认证信息并重定向到登录页 增加停止自生长闭环轮询 */
async function clearAuthAndRedirect(): Promise<void> {
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

/** 判断 token 刷新错误是否为「真正认证失效」 */
function isDefinitelyAuthFailure(error: unknown): boolean {
  // axios 错误对象：有 response 且状态码明确为 401/403 → 真认证失效
  const status = (error as AxiosError)?.response?.status
  if (status === 401 || status === 403) {
    return true
  }
  // 其余情况（无 response 的网络错误、超时 ERR_NETWORK/ETIMEDOUT、5xx）→ 暂时性故障
  return false
}

/** 创建axios实例 */
const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE_URL,
  timeout: API_TIMEOUT,
  headers: {
    'Content-Type': 'application/json',
  },
})

/** 请求拦截器 在请求发送前添加认证token */
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // 直接从 localStorage 获取 access_token
    const token = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)

    // 如果token存在，添加到请求头
    if (token && config.headers) {
      // // 某些请求（如 /auth/refresh）显式声明不带 access token（Authorization 设为空字符串），
      // 拦截器必须尊重这个声明，不覆盖。否则 refresh token 走 body，access token 却通过
      // 头抢先被后端读取，导致「期望 refresh 类型」401。
      const existing = config.headers.Authorization
      if (existing === '') {
        // 请求方明确要求不带 Authorization 头，删除它
        delete config.headers.Authorization
        return config
      }
      config.headers.Authorization = `Bearer ${token}`
    }

    return config
  },
  (error: AxiosError) => {
    // 请求错误处理
    return Promise.reject(error)
  },
)

/** 响应拦截器 处理响应错误、token刷新和自动重试 */
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
      // 检查是否是 refresh_token 刷新请求本身失败
      const isRefreshTokenRequest = originalRequest.url?.includes('/auth/refresh')

      if (isRefreshTokenRequest) {
        // refresh 请求自身 401：refresh_token 真正失效。
        // 静默处理，不报告错误，直接清除认证状态并重定向。
        if (isDefinitelyAuthFailure(error)) {
          await clearAuthAndRedirect()
        }
        return Promise.reject(error)
      }

      // 标记已重试，避免无限循环
      originalRequest._retry = true

      try {
        // // 刷新统一委托 authStore.refreshToken（单一互斥源）。
        // 并发的 401 请求会共享同一个 in-flight refresh，后端只被调用一次，
        // 消除 refresh_token 单次轮换被并发击穿导致的 race。
        // 动态 import 打破静态循环依赖（见文件顶部注释）。
        const { useAuthStore } = await import('@/stores/authStore')
        await useAuthStore.getState().refreshToken()

        // 刷新成功后从 localStorage 读最新 access token 重放原请求
        const newToken = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
        if (newToken && originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${newToken}`
        }
        return apiClient(originalRequest)
      } catch (refreshError) {
        // // 仅当后端明确返回 401/403（真认证失效）才 logout；
        // 网络错误/超时/5xx 视为暂时性故障，reject 让上层重试，保留旧 token。
        if (isDefinitelyAuthFailure(refreshError)) {
          await clearAuthAndRedirect()
        } else {
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
