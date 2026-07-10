/** 重试工具函数 */

/** 重试选项接口（兼容API调用） */
export interface RetryOptions {
  retry?: boolean
  maxRetries?: number
  retryDelay?: number
}

/** 判断错误是否可重试 */
export function isRetryableError(error: any): boolean {
  if (!error) return false

  if (error.message === 'Network Error' || !error.response) {
    return true
  }

  if (error.name === 'TypeError' && error.message.includes('fetch')) {
    return true
  }

  if (error.name === 'AbortError' || error.name === 'TimeoutError') {
    return true
  }

  const status = error.response?.status ?? error.status
  if (status) {
    // 将 429（请求过于频繁）也视为可重试错误
    return status === 429 || (status >= 500 && status < 600)
  }

  return false
}

/** 延迟函数 */
export function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 重试装饰器 */
export async function retry<T>(
  fn: () => Promise<T>,
  options: {
    maxAttempts?: number
    delayMs?: number
    shouldRetry?: (error: any) => boolean
  } = {},
): Promise<T> {
  const { maxAttempts = 3, delayMs = 1000, shouldRetry = isRetryableError } = options

  let lastError: any

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
