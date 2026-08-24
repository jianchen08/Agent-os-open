/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * @feature 认证可靠性（token 生命周期单一职责模块） | @ci frontend-test
 *
 * tokenLifecycle 是 token 存取/过期判定/互斥刷新/主动续期调度/认证失效分类的
 * 唯一实现（2026-08-21 架构收口）。本文件合并迁移三组既有语义：
 * - AC-8 TTL 边界不变量（原 authStoreTokenExpiry.test.ts，isExpired 平移）
 * - 主动续期链退避重试不断链（原 authStoreRefreshRetry.test.ts，startAutoRefresh 平移）
 * - 无凭据确定性认证失败（原 authNoCredentials.test.ts 的核心分支）
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

const mockApiRefreshToken = vi.fn()
const mockGetCurrentUser = vi.fn()

vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: (...args: unknown[]) => mockApiRefreshToken(...args),
  getCurrentUser: () => mockGetCurrentUser(),
  logout: vi.fn(),
}))

import {
  getAccessToken,
  setTokens,
  clearTokens,
  isExpired,
  isAuthFailureFromError,
  refresh,
  ensureFreshToken,
  startAutoRefresh,
  stopAutoRefresh,
} from '@/services/auth/tokenLifecycle'
import { STORAGE_KEYS } from '@/constants/storage'

const BASE_TIME = new Date('2026-01-01T00:00:00Z').getTime()

describe('tokenLifecycle: TTL 边界不变量 (isExpired)', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('TTL 边界前（刚签发）应未过期 → false', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + 10_000))
    expect(isExpired()).toBe(false)
  })

  it('临近边界（9s，TTL 内最后 1s）仍未过期 → false', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + 10_000))
    vi.advanceTimersByTime(9_000)
    expect(isExpired()).toBe(false)
  })

  it('越过边界应判定过期 → true', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + 10_000))
    vi.advanceTimersByTime(10_001)
    expect(isExpired()).toBe(true)
  })

  it('无过期时间记录 / 格式非法均视为已过期 → true', () => {
    expect(isExpired()).toBe(true)
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, 'not-a-number')
    expect(isExpired()).toBe(true)
  })
})

describe('tokenLifecycle: 存取唯一入口 (setTokens/clearTokens)', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('setTokens 写三件套（access/refresh/expiry 按 expires_in 计算）', () => {
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
    setTokens('at-1', 'rt-1', 100)
    expect(getAccessToken()).toBe('at-1')
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBe('rt-1')
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)).toBe(
      String(BASE_TIME + 100_000),
    )
    vi.useRealTimers()
  })

  it('clearTokens 清空三件套', () => {
    setTokens('at-1', 'rt-1', 100)
    clearTokens()
    expect(getAccessToken()).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)).toBeNull()
  })
})

describe('tokenLifecycle: 无凭据确定性认证失败', () => {
  beforeEach(() => {
    localStorage.clear()
    mockApiRefreshToken.mockReset()
  })

  it('refresh 无凭据时抛带 authNoCredentials 标记的错误，且被 isAuthFailureFromError 识别', async () => {
    await expect(refresh()).rejects.toMatchObject({
      message: '没有可刷新的令牌',
      authNoCredentials: true,
    })
    // 模拟 GlobalWebSocket._scheduleReconnect 的 catch 判定：错误喂给
    // isAuthFailureFromError 必须 true（走 triggerAuthExpired 弹登录，不无限重试）
    try {
      await refresh()
      expect.unreachable('无凭据时应抛错')
    } catch (e) {
      expect(isAuthFailureFromError(e)).toBe(true)
    }
  })

  it('isAuthFailureFromError 不误伤瞬时故障（普通网络错误 → false）', () => {
    expect(isAuthFailureFromError(new Error('network glitch'))).toBe(false)
  })

  it('localStorage 有 refresh_token 时不走无凭据分支（打到 API）', async () => {
    localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, 'rt-1')
    mockApiRefreshToken.mockRejectedValue(new Error('Network Error'))
    await expect(refresh()).rejects.toThrow('令牌刷新失败')
    expect(mockApiRefreshToken).toHaveBeenCalledWith('rt-1')
  })
})

describe('tokenLifecycle: 主动续期链退避重试、不断链', () => {
  const TTL_S = 100 // 100s TTL → 首次续期调度在 50s（min(TTL/2, 5min)）

  beforeEach(() => {
    localStorage.clear()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('续期瞬时失败（网络错）→ 30s 后重试 → 成功后链继续', async () => {
    setTokens('at-1', 'rt-1', TTL_S)
    startAutoRefresh()

    mockApiRefreshToken
      .mockRejectedValueOnce(new Error('Network Error')) // 第 1 次：瞬时失败
      .mockResolvedValueOnce({ access_token: 'at-2', refresh_token: 'rt-2', expires_in: TTL_S }) // 第 2 次：成功
      .mockResolvedValue({ access_token: 'at-3', refresh_token: 'rt-3', expires_in: TTL_S }) // 第 3 次起：成功（验证链持续）

    // 到首次续期点（50s）：第 1 次失败
    await vi.advanceTimersByTimeAsync(TTL_S * 1000 / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 退避 30s 内不重试
    await vi.advanceTimersByTimeAsync(29_999)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 30s 到点：第 2 次（成功，按新 TTL 重新调度 50s）
    await vi.advanceTimersByTimeAsync(1)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(2)

    // 新 TTL 的续期点触发第 3 次 → 链在持续
    await vi.advanceTimersByTimeAsync(TTL_S * 1000 / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(3)

    // 新 token 已落盘
    expect(getAccessToken()).toBe('at-3')
  })

  it('续期确定性认证失败（401）→ 不重试，链停止', async () => {
    setTokens('at-1', 'rt-1', TTL_S)
    startAutoRefresh()

    mockApiRefreshToken.mockRejectedValue(
      Object.assign(new Error('refresh token invalid'), { response: { status: 401 } }),
    )

    // 到续期点：401 → 认证失败 → 不重试
    await vi.advanceTimersByTimeAsync(TTL_S * 1000 / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 远超退避窗口后仍只有 1 次调用（链已停，交反应式路径登出）
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)
  })
})

describe('tokenLifecycle: ensureFreshToken（用前保证新鲜）', () => {
  beforeEach(() => {
    localStorage.clear()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('未过期直接返回当前 token（不打 API）', async () => {
    setTokens('at-fresh', 'rt-1', 60)
    await expect(ensureFreshToken()).resolves.toBe('at-fresh')
    expect(mockApiRefreshToken).not.toHaveBeenCalled()
  })

  it('已过期先刷新再返回新 token', async () => {
    setTokens('at-stale', 'rt-1', 1)
    vi.advanceTimersByTime(2_000) // 越过过期点
    mockApiRefreshToken.mockResolvedValue({
      access_token: 'at-new', refresh_token: 'rt-2', expires_in: 60,
    })
    await expect(ensureFreshToken()).resolves.toBe('at-new')
  })

  it('刷新失败返回 null（调用方不得拿过期 token 硬连）', async () => {
    setTokens('at-stale', 'rt-1', 1)
    vi.advanceTimersByTime(2_000)
    mockApiRefreshToken.mockRejectedValue(new Error('Network Error'))
    await expect(ensureFreshToken()).resolves.toBeNull()
  })
})
