/**
 * API 客户端 404 收敛行为测试（P4）
 *
 * 覆盖修复：
 * 1. datasource 404 是真实信号（G6-a 代理已接管 /api/v1/datasource/*，占位
 *    护栏移除）→ 与其它 404 一致降级 WARNING 上报，不再静默
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
// 导入 client.ts（触发拦截器注册；命名导入同样执行模块，副作用不变）
import { isNotFoundError } from '../client'

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

  it('datasource 端点 404 是真实信号 → 按常规 404 上报（WARNING）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/api/v1/datasource/categories/list')),
    ).rejects.toBeDefined()
    // G6-a：内核 datasource 路由已接管，404 = 数据源未命中，与其他 404 一致收敛
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [, options] = reportErrorMock.mock.calls[0] as [{}, { severity?: string }]
    const severity = options?.severity
    expect(severity).toBe('warning')
  })

  it('非业务路径 404 应上报且 severity 为 WARNING（不再 ERROR）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/api/v1/unknown-endpoint')),
    ).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [, options] = reportErrorMock.mock.calls[0] as [{}, { severity?: string }]
    const severity = options?.severity
    expect(severity).toBe('warning')
  })

  it('5xx 错误仍保持 ERROR severity（不误降级）', async () => {
    const handler = getResponseErrorHandler()
    const err = {
      config: { url: '/api/v1/sessions/abc', _retryCount: 0 },
      response: { status: 500, data: { detail: 'Internal Error' } },
      message: 'Request failed with status code 500',
    }
    await expect(handler(err)).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    const [, options] = reportErrorMock.mock.calls[0] as [{}, { severity?: string }]
    const severity = options?.severity
    expect(severity).toBe('error')
  })
})

describe('client.ts 404 机器码判定（P2-2：文案匹配退役）', () => {
  beforeEach(() => {
    reportErrorMock.mockClear()
  })

  it('404 + 内核信封 RESOURCE_NOT_FOUND → 静默（文案不参与判定）', async () => {
    const handler = getResponseErrorHandler()
    // 中文文案（旧实现的匹配信号）与英文文案均不得影响结果——机器码是唯一依据
    for (const message of ['[VALIDATION] 消息不存在', 'not found: message abc']) {
      reportErrorMock.mockClear()
      const err = {
        config: { url: '/api/v1/sessions/x/messages', _retryCount: 0 },
        response: {
          status: 404,
          data: { error: { code: 'RESOURCE_NOT_FOUND', message }, source: 'kernel' },
        },
        message: 'Request failed with status code 404',
      }
      await expect(handler(err)).rejects.toMatchObject({ code: 'RESOURCE_NOT_FOUND' })
      expect(reportErrorMock).not.toHaveBeenCalled()
    }
  })

  it('404 无信封（detail 形态，sidecar/旧后端）→ 照常上报 WARNING（分级行为不变）', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/api/v1/unknown-endpoint')),
    ).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
    expect(reportErrorMock.mock.calls[0][1]?.severity).toBe('warning')
  })

  it('请求级 optional 标记：404 静默但仍 reject 交调用方处理', async () => {
    const handler = getResponseErrorHandler()
    const err = {
      config: { url: '/ext/evaluation_service/metrics', _retryCount: 0, optional: true },
      response: { status: 404, data: { detail: 'Not Found' } },
      message: 'Request failed with status code 404',
    }
    await expect(handler(err)).rejects.toBeDefined()
    expect(reportErrorMock).not.toHaveBeenCalled()
  })

  it('URL 子串不再参与可选端点判定：无标记的 /ext/monitoring/system/metrics 404 照常上报', async () => {
    const handler = getResponseErrorHandler()
    await expect(
      handler(make404Error('/ext/monitoring/system/metrics')),
    ).rejects.toBeDefined()
    expect(reportErrorMock).toHaveBeenCalledTimes(1)
  })

  it('纯字符串响应体原文透传（旧 " - " 切分逻辑退役）', async () => {
    const handler = getResponseErrorHandler()
    const raw = 'error.model_dump - 尾段不再被切走'
    const err = {
      config: { url: '/api/v1/sessions', _retryCount: 0 },
      response: { status: 500, data: raw },
      message: 'Request failed with status code 500',
    }
    await expect(handler(err)).rejects.toMatchObject({ message: raw })
  })
})

describe('isNotFoundError 机器码判定（P2-2）', () => {
  it('内核信封机器码 RESOURCE_NOT_FOUND → true（无 response 对象的信封错误也命中）', () => {
    expect(isNotFoundError({ code: 'RESOURCE_NOT_FOUND', message: '消息不存在' })).toBe(true)
  })

  it('HTTP status / 状态回退 code 404 → true', () => {
    expect(isNotFoundError({ response: { status: 404 } })).toBe(true)
    expect(isNotFoundError({ code: '404', message: 'Request failed' })).toBe(true)
  })

  it('非 404 机器码与文案串 → false（message 文案永不参与判定）', () => {
    expect(isNotFoundError({ code: 'BAD_REQUEST', message: '包含 404 字样的文案' })).toBe(false)
    expect(isNotFoundError({ message: 'Request failed with status code 404' })).toBe(false)
    expect(isNotFoundError(undefined)).toBe(false)
  })
})
