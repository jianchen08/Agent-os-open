/** @feature FP-0.2.四 认证链加固（D1-3/D1-4/D12-7） | @ci frontend-test */
/**
 * authStore.changePassword 行为测试：
 * - 成功：落新 token 对（access 内存 / refresh sessionStorage）、
 *   mustChangePassword 清除（首登拦截放行依据）
 * - localStorage 零 token 残留（全流程）
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
const { changePasswordMock, getCurrentUserMock } = vi.hoisted(() => ({
  changePasswordMock: vi.fn(),
  getCurrentUserMock: vi.fn(),
}))
vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: vi.fn(),
  getCurrentUser: getCurrentUserMock,
  logout: vi.fn(),
  changePassword: changePasswordMock,
}))
vi.mock('@/services/authCallbacks', () => ({
  registerAuthExpiredCallback: vi.fn(),
}))
vi.mock('@/services/modules/GrowthLoop', () => ({
  restartGrowthLoop: vi.fn().mockResolvedValue(undefined),
  destroyGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn().mockResolvedValue(undefined),
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))
import { STORAGE_KEYS } from '@/constants/storage'
import { useAuthStore } from '@/stores/authStore'

describe('authStore.changePassword（D1-3/D1-4）', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    vi.clearAllMocks()
    useAuthStore.setState({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      mustChangePassword: false,
      isLoading: false,
      error: null,
    })
  })

  it('改密成功：落新 token 对、清除强制改密标记、localStorage 零 token 残留', async () => {
    // 首登强制改密状态
    useAuthStore.setState({ mustChangePassword: true, isAuthenticated: true })

    changePasswordMock.mockResolvedValue({
      access_token: 'at-new',
      refresh_token: 'rt-new',
      expires_in: 1800,
    })

    await useAuthStore.getState().changePassword('old-pass-1', 'new-pass-123')

    const s = useAuthStore.getState()
    expect(changePasswordMock).toHaveBeenCalledWith('old-pass-1', 'new-pass-123')
    expect(s.token).toBe('at-new')
    expect(s.mustChangePassword).toBe(false)
    expect(s.error).toBeNull()
    // refresh 出 localStorage（sessionStorage 承载），access 仅内存
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBe('rt-new')
  })

  it('改密失败：错误上抛、标记不清除（拦截不放行）', async () => {
    useAuthStore.setState({ mustChangePassword: true, isAuthenticated: true })
    changePasswordMock.mockRejectedValue(new Error('旧口令错误'))

    await expect(
      useAuthStore.getState().changePassword('wrong-old', 'new-pass-123'),
    ).rejects.toThrow('旧口令错误')

    expect(useAuthStore.getState().mustChangePassword).toBe(true)
  })
})
