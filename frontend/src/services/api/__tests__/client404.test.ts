/**
 * API 客户端 404 收敛行为测试（P4）
 *
 * 覆盖修复：
 * 1. isOptionalEndpoint 覆盖 /datasource/ → datasource 404 静默，不调用 reportError
 * 2. 非业务路径 404 日志级别降为 WARNING（errorSeverity=warning），不再 ERROR
 *
 * 测试方式：mock axios.create 捕获 response 拦截器的 error handler，
 * 手动构造 404 错误调用，断言 reportError 的可观察行为（是否被调 + severity）。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

// ── mock 外部依赖 ──

// vi.mock 工厂在模块顶层被提升执行（hoisted），不能在工厂内引用顶层 const（TDZ 错误）。
// 必须用 vi.hoisted 声明工厂所需的 mock 变量。
const { responseUseMock, reportErrorMock } = vi.hoisted(() => ({
  responseUseMock: vi.fn(),
  reportErrorMock: vi.fn(),
}))

// mock axios：捕获 interceptors.response.use 的 error handler
vi.mock('axios', () => ({
  default: {
    create: vi.fn(() => ({
      interceptors: {
        request: { use: vi.fn() },
        response: { use: responseUseMock },
      },
    })),
  },
}))

// mock errorReporting（外部依赖）
vi.mock('../../errorReporting', () => ({
  reportError: (...args: unknown[]) => reportErrorMock(...args),
  ErrorType: {
    NETWORK: 'network',
    VALIDATION: 'validation',
    AUTHENTICATION: 'authentication',
    AUTHORIZATION: 'authorization',
    NOT_FOUND: 'not_found',
    SERVER: 'server',
    CLIENT: 'client',
    UNKNOWN: 'unknown',
  },
  ErrorSeverity: {
    LOW: 'low',
    MEDIUM: 'medium',
    HIGH: 'high',
    CRITICAL: 'critical',
    INFO: 'info',
    WARNING: 'warning',
    ERROR: 'error',
  },
}))

// mock retry：404 不触发重试分支
vi.mock('../../../utils/retry', () => ({
  isRetryableError: vi.fn(() => false),
}))

// mock authCallbacks（401 分支才用到，避免真实导入）
vi.mock('../../authCallbacks', () => ({
  triggerAuthExpired: vi.fn(),
}))

// mock storage keys（client.ts 依赖）
vi.mock('../../../constants/storage', () => ({
  STORAGE_KEYS: {
    ACCESS_TOKEN: 'access_token',
    REFRESH_TOKEN: 'refresh_token',
    AUTH_USER: 'auth_user',
    ACCESS_TOKEN_EXPIRY: 'access_token_expiry',
  },
}))

// 导入 client.ts（触发拦截器注册）
import '../client'

function getResponseErrorHandler(): (error: unknown) => Promise<unknown> {
  // response.use(成功handler, 错误handler) → error handler 在第二个参数
  const calls = responseUseMock.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[0][1] as (error: unknown) => Promise<unknown>
}

function make404Error(url: string): unknown {
  return {
    config: { url, _retryCount: 0 },
    response: { status: 404, data: { detail: 'Not Found' } },
    message: 'Request failed with status code 404',
  }
}

describe('client.ts 404 收敛行为（P4）', () => {
  beforeEach(() => {
    reportErrorMock.mockClear()
  })

  it('datasource 端点 404 应静默，不调用 reportError', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/api/v1/datasource/categories/list')),
    ).rejects.toBeDefined()
    // 静默：不上报（isOptionalEndpoint 命中 /datasource/）
    expect(reportErrorMock).not.toHaveBeenCalled()
  })

  it('非业务路径 404 应上报且 severity 为 WARNING（不再 ERROR）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/api/v1/unknown-endpoint')),
    ).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [, , severity] = reportErrorMock.mock.calls[0]
    expect(severity).toBe('warning')
  })

  it('5xx 错误仍保持 ERROR severity（不误降级）', async () => {
    const handler = getResponseErrorHandler()
    const err = {
      config: { url: '/api/v1/threads/abc', _retryCount: 0 },
      response: { status: 500, data: { detail: 'Internal Error' } },
      message: 'Request failed with status code 500',
    }
    await expect(handler(err)).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [, , severity] = reportErrorMock.mock.calls[0]
    expect(severity).toBe('error')
  })
})
