/** @feature: FP-T12 认证可靠性 401 拦截器 | @ci: frontend-test */
/**
 * client.ts 响应拦截器 401 处理的行为测试（axios adapter 层 mock，链路真实）：
 * - 无凭据 refresh 失败（authNoCredentials）→ 正确登出，不再误判为瞬时网络故障
 *   （旧实现只看 response.status，未登录状态 401 循环永不跳登录页，2026-09-13 实测复现）
 * - 多标签并发轮换竞争：refresh 请求所用的旧值已被他 tab 轮换走（本地已落新值）
 *   → 用新值重放续链，不登出
 * - refresh 真失效（body 值与本地一致仍 401）→ 登出
 * - refresh 瞬时网络故障 → 保留凭据不登出
 *
 * mock 边界：仅 axios 传输层（defaults.adapter 路由表）与错误上报/notificationStore；
 * 拦截器、tokenLifecycle、存储读写全部真实链路。
 */
import { AxiosError, type AxiosResponse, type InternalAxiosRequestConfig } from 'axios'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { STORAGE_KEYS } from '@/constants/storage'
import {
  clearTokens,
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  stopAutoRefresh,
} from '@/services/auth/tokenLifecycle'
import { apiClient } from '../client'

const mockReportError = vi.hoisted(() => vi.fn())
vi.mock('@/services/errorReporting', () => ({
  reportError: mockReportError,
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

type Handler = (
  config: InternalAxiosRequestConfig,
) => { status: number; data?: unknown } | Promise<{ status: number; data?: unknown }>

/** method+path 路由表（path 去查询串）；handler 抛非 AxiosError 即模拟网络错误 */
const routes = new Map<string, Handler>()

function bodyToken(config: InternalAxiosRequestConfig): string | null {
  try {
    const raw = typeof config.data === 'string' ? JSON.parse(config.data) : config.data
    const token = (raw as { refresh_token?: unknown })?.refresh_token
    return typeof token === 'string' ? token : null
  } catch {
    return null
  }
}

function installAdapter(): void {
  apiClient.defaults.adapter = async (config) => {
    const path = (config.url ?? '').split('?')[0]
    const handler =
      routes.get(`${(config.method ?? 'get').toUpperCase()} ${path}`) ??
      routes.get(`* ${path}`)
    if (!handler) {
      throw new AxiosError('no route', '404', config)
    }
    const result = await handler(config)
    const response: AxiosResponse = {
      data: result.data ?? {},
      status: result.status,
      statusText: String(result.status),
      headers: {},
      config,
    }
    if (result.status >= 400) {
      throw new AxiosError('request failed', String(result.status), config, null, response)
    }
    return response
  }
}

/** 认证已过期（登出路径）的可观察副作用：reportError 收到认证过期上报 */
function expectLoggedOut(): void {
  expect(mockReportError).toHaveBeenCalledWith(
    '认证已过期，请重新登录',
    expect.objectContaining({ type: 'authentication' }),
  )
  expect(getAccessToken()).toBeNull()
}

beforeEach(() => {
  routes.clear()
  mockReportError.mockClear()
  localStorage.clear()
  sessionStorage.clear()
  clearTokens()
  installAdapter()
})

afterEach(() => {
  stopAutoRefresh()
  apiClient.defaults.adapter = undefined
})

describe('client 401 拦截器：认证失效分类（统一 isAuthFailureFromError 口径）', () => {
  it('无凭据 refresh 失败（authNoCredentials）→ 登出而非误判瞬时故障', async () => {
    // 内存有 token 但存储无 refresh 凭据（模拟存储被清/他处登出后的 401 循环现场）
    setTokens('access-1', 'rt-vanished-12345', 100)
    localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
    routes.set('GET /api/v1/sessions', () => ({ status: 401 }))

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      await expect(apiClient.get('/api/v1/sessions')).rejects.toThrow()
      expectLoggedOut()
    } finally {
      consoleSpy.mockRestore()
    }
  })

  it('refresh 真失效（body 值与本地一致仍 401）→ 登出并清空持久凭据', async () => {
    setTokens('access-1', 'rt-doomed-value-1', 100)
    routes.set('GET /api/v1/sessions', () => ({ status: 401 }))
    routes.set('POST /api/v1/auth/refresh', () => ({ status: 401 }))

    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      await expect(apiClient.get('/api/v1/sessions')).rejects.toThrow()
      expectLoggedOut()
      expect(getRefreshTokenValue()).toBeNull()
    } finally {
      consoleSpy.mockRestore()
    }
  })

  it('refresh 瞬时网络故障 → 保留凭据不登出，按暂时性故障上报', async () => {
    setTokens('access-1', 'rt-live-value-1234', 100)
    routes.set('GET /api/v1/sessions', () => ({ status: 401 }))
    routes.set('POST /api/v1/auth/refresh', () => {
      throw new Error('Network Error')
    })

    await expect(apiClient.get('/api/v1/sessions')).rejects.toThrow()
    expect(mockReportError).toHaveBeenCalledWith(
      '网络异常，认证刷新暂时失败，请检查网络后重试',
      expect.objectContaining({ type: 'network' }),
    )
    expect(getAccessToken()).toBe('access-1')
    expect(getRefreshTokenValue()).toBe('rt-live-value-1234')
  })
})

describe('client 401 拦截器：多标签并发轮换竞争（refresh token 存 localStorage 共享链）', () => {
  it('请求所用旧值已被他 tab 轮换 → 用本地新值重放续链，不登出', async () => {
    setTokens('access-1', 'rt-old-tabA-value', 100)
    let sessionsCalls = 0
    routes.set('GET /api/v1/sessions', () => {
      sessionsCalls += 1
      // 首次 401 触发刷新链；刷新成功后的重放返回 200
      return sessionsCalls === 1 ? { status: 401 } : { status: 200, data: { threads: [] } }
    })
    routes.set('POST /api/v1/auth/refresh', (config) => {
      const used = bodyToken(config)
      if (used === 'rt-old-tabA-value') {
        // 模拟另一标签已用同值完成轮换并落新值：本地存储更新在前，401 在后
        localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, 'rt-new-tabB-value')
        return { status: 401 }
      }
      // 重放（新值）→ 轮换成功，产出再下一代凭据
      return {
        status: 200,
        data: { access_token: 'access-2', refresh_token: 'rt-rotated-final', expires_in: 60 },
      }
    })

    const response = await apiClient.get('/api/v1/sessions')
    expect(response.status).toBe(200)
    // 轮换链续上而非登出：内存 access 与持久 refresh 均为最新代
    expect(getAccessToken()).toBe('access-2')
    expect(getRefreshTokenValue()).toBe('rt-rotated-final')
    expect(mockReportError).not.toHaveBeenCalledWith('认证已过期，请重新登录', expect.anything())
  })
})
