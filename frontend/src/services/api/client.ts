/** API 客户端：axios 实例 + 拦截器（认证头注入、401 刷新重放、指数退避重试、统一错误信封） */

import axios, { type AxiosError, type AxiosInstance, type InternalAxiosRequestConfig } from 'axios'
import { API_BASE_URL, API_TIMEOUT } from '../../constants/api'
import { STORAGE_KEYS } from '../../constants/storage'
import { isRetryableError } from '../../utils/retry'
import { refresh, getAccessToken, clearTokens, stopAutoRefresh } from '../auth/tokenLifecycle'
import { triggerAuthExpired } from '../authCallbacks'
import { reportError, ErrorType, ErrorSeverity } from '../errorReporting'
import type { ApiError } from '../../types/api'

// 可选端点请求级标记（axios 配置扩展）：调用服务对"前端会调但后端可能尚未
// 实现/非核心路径"的请求显式声明，拦截器据此静默其 404——判定由调用方显式
// 标记，不再做 URL 子串猜测。
declare module 'axios' {
  export interface AxiosRequestConfig {
    optional?: boolean
  }
}

/**
 * 404 机器码判定：内核统一信封稳定机器码 `RESOURCE_NOT_FOUND`
 * （kernel/crates/http/src/error.rs ApiError::NotFound），或 HTTP status /
 * 状态回退 code 404（拦截器对无信封响应以状态码字符串回填 code）。
 * 刻意不做 message 文案匹配——文案随版本/语言漂移，机器码是唯一稳定契约。
 */
export function isNotFoundError(error: unknown): boolean {
  const e = error as { response?: { status?: number }; code?: string } | undefined
  if (!e) return false
  if (e.response?.status === 404) return true
  return e.code === 'RESOURCE_NOT_FOUND' || e.code === '404'
}

// NOTE: token 生命周期（互斥刷新/存取/续期调度）统一由 tokenLifecycle 提供
// （架构收口）。tokenLifecycle 对本文件的依赖是动态 import
// （refresh → services/api/auth → 本文件），因此本文件可静态 import 它，
// 不构成静态循环依赖。

/** 清除认证状态：停止 growth loop 轮询、清令牌、通知 store 并重定向登录页 */
async function clearAuthAndRedirect(): Promise<void> {
  try {
    const { destroyGrowthLoop } = await import('../modules/GrowthLoop')
    destroyGrowthLoop()
  } catch {
    // 模块未加载过，忽略
  }

  // 仅清除令牌与用户信息，禁止清理任何工作区状态
  // （LAST_ACTIVE_SESSION / pipeline-messages / agent-tabs / layout-mode 等保留，供重登后恢复）
  stopAutoRefresh()
  clearTokens()
  localStorage.removeItem(STORAGE_KEYS.AUTH_USER)

  triggerAuthExpired()

  reportError('认证已过期，请重新登录', {
    type: ErrorType.AUTHENTICATION,
    severity: ErrorSeverity.WARNING,
    code: '401',
  })

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
    // token 读取走 tokenLifecycle 唯一入口
    const token = getAccessToken()

    if (token && config.headers) {
      // 某些请求（如 /auth/refresh）显式声明不带 access token（Authorization 设为空字符串），
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
    return Promise.reject(error)
  },
)

/** 响应拦截器 处理响应错误、token刷新和自动重试 */
apiClient.interceptors.response.use(
  (response) => {
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

    if (originalRequest._retryCount === undefined) {
      originalRequest._retryCount = 0
    }

    // 如果是401错误且未重试过，尝试刷新token
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
        // 刷新统一委托 tokenLifecycle.refresh（单一互斥源）。
        // 并发的 401 请求会共享同一个 in-flight refresh，后端只被调用一次，
        // 消除 refresh_token 单次轮换被并发击穿导致的 race。
        await refresh()

        // 刷新成功后取最新 access token 重放原请求
        const newToken = getAccessToken()
        if (newToken && originalRequest.headers) {
          originalRequest.headers.Authorization = `Bearer ${newToken}`
        }
        return apiClient(originalRequest)
      } catch (refreshError) {
        // 仅当后端明确返回 401/403（真认证失效）才 logout；
        // 网络错误/超时/5xx 视为暂时性故障，reject 让上层重试，保留旧 token。
        if (isDefinitelyAuthFailure(refreshError)) {
          await clearAuthAndRedirect()
        } else {
          reportError('网络异常，认证刷新暂时失败，请检查网络后重试', {
            type: ErrorType.NETWORK,
            severity: ErrorSeverity.WARNING,
            showToast: false,
          })
        }
        return Promise.reject(refreshError)
      }
    }

    // 构建API错误对象
    // 处理 detail 可能是对象的情况（后端返回结构化错误）
    const responseData = error.response?.data as any
    let errorMessage: string

    if (typeof responseData === 'string') {
      errorMessage = responseData
    } else if (typeof responseData?.error === 'object' && responseData?.error !== null) {
      // 内核统一信封 {error: {code, message, source, retryable}}（单一真值源
      // kernel/crates/http/src/error.rs + config/error_codes.json）：code 为
      // 稳定机器码（如 BAD_REQUEST / RESOURCE_NOT_FOUND / INTERNAL_ERROR），
      // 非 HTTP 状态字符串；message 为业务文案。
      // 对象形态优先级最高——axios 的通用 message 无业务信息。
      if (typeof responseData.error.message === 'string') {
        errorMessage = responseData.error.message
      } else if (typeof responseData.error.code === 'string') {
        errorMessage = responseData.error.code
      } else {
        errorMessage = '请求失败'
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
      code:
        (typeof responseData?.error?.code === 'string' ? responseData.error.code : undefined) ||
        error.response?.status?.toString() ||
        error.code ||
        'UNKNOWN_ERROR',
      message: errorMessage,
      // 统一错误信封（config/error_codes.json 单一真值源）：source 供渲染
      // 来源标签，retryable 驱动重试按钮；旧后端无这些字段时保持 undefined。
      source:
        typeof responseData?.error?.source === 'string'
          ? (responseData.error.source as ApiError['source'])
          : undefined,
      retryable:
        typeof responseData?.error?.retryable === 'boolean'
          ? responseData.error.retryable
          : undefined,
      details: error.response?.data,
    }

    const shouldRetry = isRetryableError(error) && originalRequest._retryCount < 2

    if (shouldRetry) {
      originalRequest._retryCount++

      // 计算延迟时间（指数退避）
      const delayTime = Math.min(1000 * Math.pow(2, originalRequest._retryCount - 1), 5000)

      // 报告重试信息（不显示Toast，只记录到控制台）
      reportError(`请求失败，${delayTime}ms 后进行第 ${originalRequest._retryCount} 次重试`, {
        type: ErrorType.NETWORK,
        severity: ErrorSeverity.INFO,
        showToast: false,
        code: apiError.code,
      })

      await new Promise((resolve) => setTimeout(resolve, delayTime))
      return apiClient(originalRequest)
    }

    // 404 静默收敛：内核统一信封稳定机器码 RESOURCE_NOT_FOUND（http/error.rs
    // ApiError::NotFound）。覆盖消息读取的竞态窗口——消息刚创建未落库 / 已被
    // 删除 / 临时消息 ID 未更新 / 子管道消息尚不存在（子 Agent 未开始执行）。
    // 判定只认机器码，文案不参与（文案随版本漂移）；业务失败仍经
    // Promise.reject 交给调用方处理，此处只收敛告警噪音。
    const shouldSilentIgnore =
      error.response?.status === 404 && responseData?.error?.code === 'RESOURCE_NOT_FOUND'

    // 必须 reject Error 实例：全仓消费方惯用 `error instanceof Error ? error.message
    // : 兜底文案` 读取原因，普通对象会让 message 退化为兜底文案（2026-09-10 登录页
    // 只显示「登录失败」实锤）。附加字段（code/source/retryable/details）随 assign 保留。
    const rejectWithError = () => Object.assign(new Error(errorMessage), apiError)

    if (shouldSilentIgnore) {
      // 静默处理，不上报错误
      return Promise.reject(rejectWithError())
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

    // 可选端点：调用服务经请求级 optional 标记显式声明（"前端会调但后端可能
    // 尚未实现/非核心路径"），404 不上报刷屏；真实业务失败仍通过
    // Promise.reject 交给调用方处理
    // （floating-chat 已退役——前端改本地实现，无后端调用）
    const isOptionalEndpoint = originalRequest.optional === true
    // datasource 占位护栏已移除（G6-a）：/api/v1/datasource/{*rest} 由内核真实路由接管，
    // 404 即真实未命中（前端 fetchDatasourceOptions 正常注册表兜底空选项）。

    if (!isOptionalEndpoint) {
      // 非可选端点的 404 视为需要排查的异常路径，DEV 下输出 URL 便于快速定位。
      if (import.meta.env.DEV && error.response?.status === 404) {
        console.warn(`[API-404] url=${String(originalRequest.url ?? '')} status=404`)
      }
      reportError(apiError.message, {
        type: errorType,
        // P4: 404 多为非业务路径（端点未实现/资源不存在），降级为 WARNING 收敛告警噪音；
        // 认证失败保持 WARNING；5xx/网络错误保持 ERROR
        severity:
          error.response?.status === 404
            ? ErrorSeverity.WARNING
            : errorType === ErrorType.AUTHENTICATION
              ? ErrorSeverity.WARNING
              : ErrorSeverity.ERROR,
        code: apiError.code,
        details: apiError.details,
        // 统一错误信封来源（config/error_codes.json）：通知中心渲染来源标签
        source: apiError.source,
      })
    }

    return Promise.reject(rejectWithError())
  },
)

export default apiClient

// 同时导出默认导出和命名导出
export { apiClient }
