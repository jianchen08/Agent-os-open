/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * API 客户端内核错误信封解析测试（2026-08-22 错误透传收口）
 *
 * 内核统一信封 {error: {code, message}}（kernel/crates/http/src/error.rs）：
 * 拦截器按对象形态优先提取业务文案 + 信封 code 优先（不解析信封时 message
 * 会落 axios 通用文本、code 丢失）。
 *
 * 测试方式：mock axios.create 捕获 response 拦截器的 error handler，
 * 手动构造带信封/不带信封的错误，断言 reportError 收到的 message/code。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { responseUseMock, reportErrorMock } = vi.hoisted(() => ({
  responseUseMock: vi.fn(),
  reportErrorMock: vi.fn(),
}))

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

vi.mock('../../../utils/retry', () => ({
  isRetryableError: vi.fn(() => false),
}))

vi.mock('../../authCallbacks', () => ({
  triggerAuthExpired: vi.fn(),
}))

vi.mock('../../../constants/storage', () => ({
  STORAGE_KEYS: {
    ACCESS_TOKEN: 'access_token',
    REFRESH_TOKEN: 'refresh_token',
    AUTH_USER: 'auth_user',
    ACCESS_TOKEN_EXPIRY: 'access_token_expiry',
  },
}))

import '../client'

function getResponseErrorHandler(): (error: unknown) => Promise<unknown> {
  const calls = responseUseMock.mock.calls
  expect(calls.length).toBeGreaterThan(0)
  return calls[0][1] as (error: unknown) => Promise<unknown>
}

function makeError(status: number, url: string, data: unknown): unknown {
  return {
    config: { url, _retryCount: 0 },
    response: { status, data },
    message: 'Request failed with status code ' + status,
  }
}

describe('client.ts 内核错误信封 {error:{code,message}} 解析（2026-08-22）', () => {
  beforeEach(() => {
    reportErrorMock.mockClear()
  })

  it('信封 message 应被提取为业务文案（不再落 axios 通用文本）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(makeError(400, '/api/v1/sessions', { error: { code: '400', message: '会话创建失败：参数不合法' } })),
    ).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [message] = reportErrorMock.mock.calls[0]
    expect(message).toBe('会话创建失败：参数不合法')
  })

  it('信封 code 优先于 HTTP 状态码（同 500 响应下用业务 code）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(makeError(500, '/api/v1/sessions/abc', { error: { code: 'ENGINE_FAILED', message: '引擎执行失败' } })),
    ).rejects.toBeDefined()
    const [message, options] = reportErrorMock.mock.calls[0] as [string, { code?: string }]
    expect(message).toBe('引擎执行失败')
    expect(options.code).toBe('ENGINE_FAILED')
  })

  it('统一错误信封 source/retryable 透传（config/kernel/error_codes.json 单一真值源）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(
        makeError(500, '/api/v1/sessions/abc', {
          error: {
            code: 'INTERNAL_ERROR',
            message: 'io error: 磁盘写入失败',
            source: 'kernel',
            retryable: true,
          },
        }),
      ),
    ).rejects.toBeDefined()
    const [, options] = reportErrorMock.mock.calls[0] as [
      string,
      { code?: string; source?: string },
    ]
    expect(options.code).toBe('INTERNAL_ERROR')
    expect(options.source).toBe('kernel')
  })

  it('旧后端无 source 字段时保持 undefined（兼容不炸）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(makeError(400, '/api/v1/sessions', { error: { code: '400', message: '参数校验失败' } })),
    ).rejects.toBeDefined()
    const [, options] = reportErrorMock.mock.calls[0] as [{}, { source?: string }]
    expect(options.source).toBeUndefined()
  })

  it('非信封格式（{detail}）保持既有解析路径不变', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(makeError(422, '/api/v1/sessions', { detail: '校验失败' })),
    ).rejects.toBeDefined()
    const [message, options] = reportErrorMock.mock.calls[0] as [string, { code?: string }]
    expect(message).toBe('校验失败')
    expect(options.code).toBe('422')
  })

  it('reject 的必须是 Error 实例，message 携带信封业务文案（2026-09-10 登录页只显示「登录失败」回归）', async () => {
    const handler = getResponseErrorHandler()
    const rejected = await handler(
      makeError(400, '/api/v1/auth/login', {
        error: { code: 'BAD_REQUEST', message: '用户名或密码错误' },
      }),
    ).then(
      () => {
        throw new Error('应当 reject')
      },
      (e: unknown) => e,
    )
    // 全仓消费方惯用 `error instanceof Error ? error.message : 兜底文案`，
    // 普通对象会退化成兜底文案（具体原因只进控制台不上 UI）
    expect(rejected).toBeInstanceOf(Error)
    expect((rejected as Error).message).toBe('用户名或密码错误')
    expect((rejected as { code?: string }).code).toBe('BAD_REQUEST')
  })
})
