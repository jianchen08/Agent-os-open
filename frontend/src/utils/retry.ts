/** 重试工具函数 */

/** 重试选项接口（兼容API调用） */
export interface RetryOptions {
  retry?: boolean
  maxRetries?: number
  retryDelay?: number
}

/** 重试判定的错误形状（AxiosError / DOMException / 普通 Error 的公共子面） */
interface RetryableErrorShape {
  message?: unknown
  name?: unknown
  status?: unknown
  response?: { status?: unknown } | null
}

/** unknown → 错误形状（非对象一律空形状，保持与旧 any 实现相同的判定入口） */
function asErrorShape(error: unknown): RetryableErrorShape {
  if (typeof error !== 'object' || error === null) return {}
  return error as RetryableErrorShape
}

/** 判断错误是否可重试 */
export function isRetryableError(error: unknown): boolean {
  const e = asErrorShape(error)
  // falsy 值不可重试；无响应对象视为网络级失败可重试
  if (!error) return false

  if (e.message === 'Network Error' || !e.response) {
    return true
  }

  if (e.name === 'TypeError' && typeof e.message === 'string' && e.message.includes('fetch')) {
    return true
  }

  if (e.name === 'AbortError' || e.name === 'TimeoutError') {
    return true
  }

  const status = e.response?.status ?? e.status
  if (status) {
    // 将 429（请求过于频繁）也视为可重试错误
    return status === 429 || (typeof status === 'number' && status >= 500 && status < 600)
  }

  return false
}

/** 延迟函数 */
function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 重试装饰器 */
export async function retry<T>(
  fn: () => Promise<T>,
  options: {
    maxAttempts?: number
    delayMs?: number
    shouldRetry?: (error: unknown) => boolean
  } = {},
): Promise<T> {
  const { maxAttempts = 3, delayMs = 1000, shouldRetry = isRetryableError } = options

  let lastError: unknown

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn()
    } catch (error) {
      lastError = error

      if (attempt < maxAttempts && shouldRetry(error)) {
        await delay(delayMs * attempt)
        continue
      }

      throw error
    }
  }

  throw lastError
}

/** 带重试的请求包装器（兼容现有API调用） */
export async function requestWithRetry<T>(
  requestFn: () => Promise<T>,
  options: RetryOptions = {},
): Promise<T> {
  const { retry: enableRetry = false, maxRetries = 3, retryDelay = 1000 } = options

  if (!enableRetry) {
    return requestFn()
  }

  return retry(requestFn, {
    maxAttempts: maxRetries,
    delayMs: retryDelay,
    shouldRetry: isRetryableError,
  })
}
