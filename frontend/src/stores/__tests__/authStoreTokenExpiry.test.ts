/**
 * @feature FP-0.2.八 多租户/认证 | @vision V4 多用户 | @audit T5#6 | @ci frontend-test
 *
 * AC-8 Token TTL 时序不变量（T5#6 修复）：
 * 原 e2e ac_validation.spec.ts:149 用 page.waitForTimeout(10_000) 真实墙钟等待验证
 * "登录后 10s 仍在线"——既无法断言 TTL 边界，又拖慢套件。本单测用 vi.useFakeTimers
 * 注入可控时钟，精确断言 checkTokenExpiration() 在 TTL 边界前后的真值：
 * 不依赖真实延迟、不依赖运行中的 kernel/前端服务。
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'

// 避免拉起真实网络链路：mock 认证 API 与过期回调注册
vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refresh: vi.fn(),
  getCurrentUser: vi.fn(),
  logout: vi.fn(),
}))
vi.mock('@/services/authCallbacks', () => ({
  registerAuthExpiredCallback: vi.fn(),
}))

import { useAuthStore } from '@/stores/authStore'
import { STORAGE_KEYS } from '@/constants/storage'

// 固定基准时钟，避免真实 Date.now() 干扰边界断言
const BASE_TIME = new Date('2026-01-01T00:00:00Z').getTime()
const TTL_MS = 10_000 // 复刻 AC-8 的 10s TTL

describe('AC-8 Token TTL 时序不变量 (checkTokenExpiration)', () => {
  beforeEach(() => {
    localStorage.clear()
    useAuthStore.getState().clearError?.()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('TTL 边界前（刚签发）应未过期 → false', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + TTL_MS))
    expect(useAuthStore.getState().checkTokenExpiration()).toBe(false)
  })

  it('临近边界（9s，TTL 内最后 1s）仍未过期 → false', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + TTL_MS))
    vi.advanceTimersByTime(TTL_MS - 1000)
    expect(useAuthStore.getState().checkTokenExpiration()).toBe(false)
  })

  it('越过边界（>10s）应判定过期 → true', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, String(BASE_TIME + TTL_MS))
    vi.advanceTimersByTime(TTL_MS + 1)
    expect(useAuthStore.getState().checkTokenExpiration()).toBe(true)
  })

  it('无过期时间记录视为已过期 → true', () => {
    expect(useAuthStore.getState().checkTokenExpiration()).toBe(true)
  })

  it('过期时间格式非法视为已过期 → true', () => {
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, 'not-a-number')
    expect(useAuthStore.getState().checkTokenExpiration()).toBe(true)
  })
})
