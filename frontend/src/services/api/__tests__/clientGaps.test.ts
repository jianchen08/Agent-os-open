// @feature: FP-T12 补测 | @ci: frontend-test
/** @feature 覆盖率冲刺批九（前端 API 层） | @ci frontend-test */
/**
 * client.ts 覆盖缺口补测（拦截器 handler 捕获式 mock）：
 * - 请求拦截器错误处理分支
 * - 错误文案解析链缺口（信封 message/code 非字符串、detail 对象、全兜底）
 * - 错误分类缺口（401/403 → AUTHENTICATION、无响应 → NETWORK、429 → VALIDATION）
 * - 401 刷新分支缺口：重放带新 Bearer / 无新 token 不改写头、refresh 自身 401
 *   的 body 解析容错（非法 JSON / 非字符串 token → 视为无法续链 → 登出）、
 *   非认证失败分类不登出、/login 路径不重定向
 *
 * mock 边界：axios 实例（捕获拦截器 handler）、tokenLifecycle、错误上报、
 * GrowthLoop/authCallbacks；信封解析与分类逻辑全部真实链路。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const h = vi.hoisted(() => {
  const requestUse = vi.fn()
  const responseUse = vi.fn()
  // axios 实例是可调用对象（拦截器重放 return apiClient(originalRequest)）：
  // 默认给 resolved，避免消费侧 .catch 崩溃
  const instance = Object.assign(
    vi.fn(() => Promise.resolve({ status: 200, data: {} })),
    {
      interceptors: {
        request: { use: requestUse },
        response: { use: responseUse },
      },
    },
  )
  return {
    requestUse,
    responseUse,
    instance,
    refresh: vi.fn(() => Promise.resolve()),
    getAccessToken: vi.fn(() => null),
    getRefreshTokenValue: vi.fn(() => null),
    clearTokens: vi.fn(),
    stopAutoRefresh: vi.fn(),
    isAuthFailureFromError: vi.fn(() => true),
    triggerAuthExpired: vi.fn(),
    reportError: vi.fn(),
  }
})

vi.mock('axios', () => ({
  default: { create: vi.fn(() => h.instance) },
}))
vi.mock('@/services/auth/tokenLifecycle', () => ({
  refresh: h.refresh,
  getAccessToken: h.getAccessToken,
  getRefreshTokenValue: h.getRefreshTokenValue,
  clearTokens: h.clearTokens,
  stopAutoRefresh: h.stopAutoRefresh,
  isAuthFailureFromError: h.isAuthFailureFromError,
}))
vi.mock('@/services/authCallbacks', () => ({ triggerAuthExpired: h.triggerAuthExpired }))
vi.mock('@/services/errorReporting', () => ({
  reportError: h.reportError,
  ErrorType: {
    AUTHENTICATION: 'authentication',
    NETWORK: 'network',
    SERVER: 'server',
    VALIDATION: 'validation',
    CLIENT: 'client',
  },
  ErrorSeverity: { INFO: 'info', WARNING: 'warning', ERROR: 'error' },
}))
vi.mock('@/services/modules/GrowthLoop', () => ({
  destroyGrowthLoop: vi.fn(),
  restartGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn(),
}))
vi.mock('@/utils/retry', () => ({ isRetryableError: vi.fn(() => false) }))

import '../client'

// 拦截器 handler 在模块导入时注册一次；beforeEach 的 clearAllMocks 会清空调用
// 记录，故必须在模块加载期（导入后立即）捕获 handler 引用
const responseErrorHandler = h.responseUse.mock.calls[0][1] as (error: unknown) => Promise<unknown>
const requestErrorHandler = h.requestUse.mock.calls[0][1] as (error: unknown) => Promise<unknown>

interface ErrorShape {
  status?: number
  data?: unknown
  url?: string
  message?: string
  code?: string
  retry?: boolean
  retryCount?: number
  headers?: Record<string, string>
  body?: unknown
}

function makeError(shape: ErrorShape): unknown {
  return {
    config: {
      url: shape.url ?? '/api/v1/sessions',
      _retry: shape.retry,
      _retryCount: shape.retryCount ?? 2,
      ...(shape.headers ? { headers: shape.headers } : {}),
      ...(shape.body !== undefined ? { data: shape.body } : {}),
    },
    ...(shape.status !== undefined ? { response: { status: shape.status, data: shape.data } } : {}),
    message: shape.message,
    ...(shape.code ? { code: shape.code } : {}),
  }
}

async function rejectedOf(p: Promise<unknown>): Promise<Record<string, unknown>> {
  return (await p.then(
    () => {
      throw new Error('应当 reject')
    },
    (e: unknown) => e as Record<string, unknown>,
  )) as Record<string, unknown>
}

describe('client.ts 覆盖缺口（批九）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // vi.clearAllMocks 清实现前的记录但保留 mockImplementation；显式复位默认行为
    h.refresh.mockImplementation(() => Promise.resolve())
    h.getAccessToken.mockReturnValue(null)
    h.getRefreshTokenValue.mockReturnValue(null)
    h.isAuthFailureFromError.mockReturnValue(true)
    h.instance.mockImplementation(() => Promise.resolve({ status: 200, data: {} }))
    localStorage.clear()
    window.history.pushState({}, '', '/')
  })

  afterEach(() => {
    window.history.pushState({}, '', '/')
  })

  describe('请求拦截器', () => {
    it('错误处理器原样拒绝（不打错误码补丁）', async () => {
      const handler = requestErrorHandler
      const boom = new Error('拦截器上游错误')
      await expect(handler(boom)).rejects.toBe(boom)
    })
  })

  describe('错误文案解析链缺口', () => {
    it('信封 message 非字符串但 code 为字符串 → 以机器码作文案', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(
          makeError({ status: 409, data: { error: { code: 'ORDER_CONFLICT', message: 42 } } }),
        ),
      )
      expect(rejected.message).toBe('ORDER_CONFLICT')
      expect(rejected.code).toBe('ORDER_CONFLICT')
      // 该分支不上报？——非 404 照常上报一次
      expect(h.reportError).toHaveBeenCalledTimes(1)
    })

    it('信封 message 与 code 均非字符串 → 兜底「请求失败」，code 回退 HTTP 状态', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(makeError({ status: 409, data: { error: { code: 1, message: 2 } } })),
      )
      expect(rejected.message).toBe('请求失败')
      expect(rejected.code).toBe('409')
    })

    it('detail 为对象且带 message → 取 detail.message', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(makeError({ status: 422, data: { detail: { message: '对象 detail 文案' } } })),
      )
      expect(rejected.message).toBe('对象 detail 文案')
      expect(rejected.code).toBe('422')
    })

    it('无可识别字段且无 message → 终极兜底「请求失败」', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(handler(makeError({ status: 500, data: {} })))
      expect(rejected.message).toBe('请求失败')
      expect(rejected.code).toBe('500')
    })

    it('detail 为对象但无 message → 回退 error.message 兜底', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(
          makeError({ status: 502, data: { detail: { other: 1 } }, message: 'Axios 通用错误' }),
        ),
      )
      expect(rejected.message).toBe('Axios 通用错误')
    })
  })

  describe('错误分类与上报 severity 缺口', () => {
    it('403（已重试标记）→ AUTHENTICATION 类型 + WARNING severity，信封 code 透传', async () => {
      const handler = responseErrorHandler
      await rejectedOf(
        handler(
          makeError({
            status: 403,
            retry: true,
            data: { error: { code: 'FORBIDDEN', message: '无权限' } },
          }),
        ),
      )
      expect(h.reportError).toHaveBeenCalledWith(
        '无权限',
        expect.objectContaining({ type: 'authentication', severity: 'warning', code: 'FORBIDDEN' }),
      )
    })

    it('无响应网络错误 → NETWORK 类型 + ERROR severity，code 回退 error.code', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(makeError({ code: 'ECONNABORTED', message: 'timeout of 30000ms exceeded' })),
      )
      expect(h.reportError).toHaveBeenCalledWith(
        'timeout of 30000ms exceeded',
        expect.objectContaining({ type: 'network', severity: 'error', code: 'ECONNABORTED' }),
      )
      expect(rejected.code).toBe('ECONNABORTED')
    })

    it('429 → VALIDATION 类型（isRetryableError 为 false 时不重试）', async () => {
      const handler = responseErrorHandler
      const rejected = await rejectedOf(
        handler(makeError({ status: 429, data: {}, message: 'too many requests' })),
      )
      expect(h.reportError).toHaveBeenCalledWith(
        'too many requests',
        expect.objectContaining({ type: 'validation', severity: 'error', code: '429' }),
      )
      // 未进入重试：不重放
      expect(h.instance).not.toHaveBeenCalled()
      expect(rejected.code).toBe('429')
    })

    it('DEV 关闭时非可选端点 404 不输出排查日志（上报行为不变）', async () => {
      vi.stubEnv('DEV', false)
      const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      try {
        const handler = responseErrorHandler
        await rejectedOf(handler(makeError({ status: 404, data: { detail: 'Not Found' } })))
        expect(warnSpy).not.toHaveBeenCalled()
        expect(h.reportError).toHaveBeenCalledTimes(1)
      } finally {
        warnSpy.mockRestore()
        vi.unstubAllEnvs()
      }
    })
  })

  describe('401 刷新与重放分支', () => {
    it('刷新成功 → 重放原请求并改写 Bearer 为新 token', async () => {
      h.getAccessToken.mockReturnValue('tok-new')
      const handler = responseErrorHandler
      const replayResponse = { status: 200, data: { ok: 1 } }
      h.instance.mockImplementation(() => Promise.resolve(replayResponse))

      const result = await handler(
        makeError({
          status: 401,
          headers: { Authorization: 'Bearer old' },
          retryCount: 0,
        }),
      )

      expect(h.refresh).toHaveBeenCalledTimes(1)
      expect(h.instance).toHaveBeenCalledTimes(1)
      const replayed = h.instance.mock.calls[0][0] as { headers: Record<string, string> }
      expect(replayed.headers.Authorization).toBe('Bearer tok-new')
      expect(result).toBe(replayResponse)
    })

    it('刷新成功但拿不到新 token → 仍重放且不改写既有头', async () => {
      h.getAccessToken.mockReturnValue(null)
      const handler = responseErrorHandler

      await handler(
        makeError({
          status: 401,
          headers: { Authorization: 'Bearer stale' },
          retryCount: 0,
        }),
      )

      const replayed = h.instance.mock.calls[0][0] as { headers: Record<string, string> }
      expect(h.refresh).toHaveBeenCalledTimes(1)
      expect(replayed.headers.Authorization).toBe('Bearer stale')
    })

    it('刷新失败且分类为认证失败 → 登出（/login 路径不再重定向）', async () => {
      h.refresh.mockImplementation(() => Promise.reject(new Error('refresh boom')))
      window.history.pushState({}, '', '/login')
      localStorage.setItem('auth_user', '{"id":1}')

      const handler = responseErrorHandler
      await expect(
        handler(makeError({ status: 401, headers: { Authorization: 'Bearer x' }, retryCount: 0 })),
      ).rejects.toThrow('refresh boom')

      expect(h.stopAutoRefresh).toHaveBeenCalled()
      expect(h.clearTokens).toHaveBeenCalled()
      expect(localStorage.getItem('auth_user')).toBeNull()
      expect(h.triggerAuthExpired).toHaveBeenCalledTimes(1)
      expect(h.reportError).toHaveBeenCalledWith(
        '认证已过期，请重新登录',
        expect.objectContaining({ type: 'authentication', code: '401' }),
      )
      // 不重定向：不再改写 location
      expect(window.location.pathname).toBe('/login')
      expect(h.instance).not.toHaveBeenCalled()
    })

    it('refresh 请求自身 401 + body 非法 JSON → 无法判定竞争 → 登出且不触发 refresh()', async () => {
      // 断言点在登出副作用而非重定向（重定向弧由 client401 真实链路覆盖），
      // 置于 /login 路径避免 jsdom 无效导航噪音
      window.history.pushState({}, '', '/login')
      const handler = responseErrorHandler
      await expect(
        handler(
          makeError({
            status: 401,
            url: '/api/v1/auth/refresh',
            body: '{not-json',
            retryCount: 0,
          }),
        ),
      ).rejects.toThrow()

      expect(h.refresh).not.toHaveBeenCalled()
      expect(h.triggerAuthExpired).toHaveBeenCalledTimes(1)
      expect(h.clearTokens).toHaveBeenCalled()
    })

    it('refresh 请求自身 401 + body 对象但 refresh_token 非字符串 → 同样视为无可用值 → 登出', async () => {
      window.history.pushState({}, '', '/login')
      const handler = responseErrorHandler
      await expect(
        handler(
          makeError({
            status: 401,
            url: '/api/v1/auth/refresh',
            body: { refresh_token: 42 },
            retryCount: 0,
          }),
        ),
      ).rejects.toThrow()

      expect(h.triggerAuthExpired).toHaveBeenCalledTimes(1)
    })

    it('refresh 请求自身 401 但分类为非认证失败 → 不登出、原错误直接拒绝', async () => {
      h.isAuthFailureFromError.mockReturnValue(false)
      const handler = responseErrorHandler
      const err = makeError({
        status: 401,
        url: '/api/v1/auth/refresh',
        body: '{"refresh_token":"rt-1"}',
        retryCount: 0,
      })

      await expect(handler(err)).rejects.toBe(err)
      expect(h.refresh).not.toHaveBeenCalled()
      expect(h.triggerAuthExpired).not.toHaveBeenCalled()
      expect(h.clearTokens).not.toHaveBeenCalled()
    })
  })
})
