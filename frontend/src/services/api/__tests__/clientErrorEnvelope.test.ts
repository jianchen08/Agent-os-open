/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * API 客户端内核错误信封解析测试（2026-08-22 错误透传收口）
 *
 * 内核统一信封 {error: {code, message}}（kernel/crates/http/src/error.rs）：
 * 拦截器此前不解析 error.code 字段、message 落 axios 通用文本；
 * 本次改为对象形态优先提取业务文案 + 信封 code 优先。
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
    const [message, , , context] = reportErrorMock.mock.calls[0]
    expect(message).toBe('引擎执行失败')
    expect(context.code).toBe('ENGINE_FAILED')
  })

  it('非信封格式（{detail}）保持既有解析路径不变', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(makeError(422, '/api/v1/sessions', { detail: '校验失败' })),
    ).rejects.toBeDefined()
    const [message, , , context] = reportErrorMock.mock.calls[0]
    expect(message).toBe('校验失败')
    expect(context.code).toBe('422')
  })
})
