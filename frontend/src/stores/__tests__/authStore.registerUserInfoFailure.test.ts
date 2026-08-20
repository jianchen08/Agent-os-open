/** @feature FP-兜底反模式修复.FE12 注册流程不伪造 unknown 用户 @ci frontend-test */
/**
 * register 获取用户信息失败：对齐 login 路径——
 * 不伪造 id:'unknown' 用户写 localStorage，置错误要求重新登录。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { registerMock, getCurrentUserMock } = vi.hoisted(() => ({
  registerMock: vi.fn(),
  getCurrentUserMock: vi.fn(),
}))

vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: registerMock,
  refresh: vi.fn(),
  getCurrentUser: getCurrentUserMock,
  logout: vi.fn(),
}))
vi.mock('@/services/authCallbacks', () => ({
  registerAuthExpiredCallback: vi.fn(),
}))
// 注册成功后会动态 import restartGrowthLoop——mock 掉避免真实网络链路
vi.mock('@/services/modules/GrowthLoop', () => ({
  restartGrowthLoop: vi.fn().mockResolvedValue(undefined),
  destroyGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn().mockResolvedValue(undefined),
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

import { useAuthStore } from '@/stores/authStore'
import { STORAGE_KEYS } from '@/constants/storage'

describe('register：获取用户信息失败不伪造 unknown 用户（FE12）', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
    useAuthStore.setState({
      user: null, token: null, refreshTokenValue: null,
      isAuthenticated: false, isLoading: false, error: null,
    })

    registerMock.mockResolvedValue({
      access_token: 'token-1',
      refresh_token: 'refresh-1',
      expires_in: 3600,
    })
  })

  it('getCurrentUser 失败：user=null、不持久化 AUTH_USER、error 提示重登', async () => {
    getCurrentUserMock.mockRejectedValue(new Error('profile endpoint down'))

    await useAuthStore.getState().register('fe12user', 'pass1234', 'fe12@example.com')

    const s = useAuthStore.getState()
    expect(s.user).toBeNull()
    expect(s.error).toContain('注册成功但获取用户信息失败')
    expect(s.error).toContain('请重新登录')
    // 关键断言：不把 id:'unknown' 的伪造用户写进 localStorage
    expect(localStorage.getItem(STORAGE_KEYS.AUTH_USER)).toBeNull()
  })

  it('getCurrentUser 成功：正常持久化真实用户', async () => {
    getCurrentUserMock.mockResolvedValue({ id: 'u-123', username: 'fe12user' })

    await useAuthStore.getState().register('fe12user', 'pass1234', 'fe12@example.com')

    const s = useAuthStore.getState()
    expect(s.user?.id).toBe('u-123')
    expect(s.error).toBeNull()
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.AUTH_USER)!).id).toBe('u-123')
  })
})
