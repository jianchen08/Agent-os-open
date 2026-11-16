/** @feature: FP-0.2.登录失败分类 authStore 登录失败横幅 | @ci: frontend-test */
/**
 * authStore 登录失败横幅文案三分类（S5-A②/S5-D①）：
 * - 429 限速锁=锁定文案（含 Retry-After 秒数，不诱导继续输入）
 * - 凭据错=后端原文（用户名或密码错误）
 * - 网络失败=无法连接内核 + 附当前目标基址（本机/远程可辨别）
 * - 重抛原错误（附着 code 等分类字段）——LoginModal 自动链据此识别 429 终态停链
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const authApi = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: vi.fn(),
  getCurrentUser: vi.fn(),
  logout: vi.fn(),
  changePassword: vi.fn(),
}))

const tl = vi.hoisted(() => ({
  setTokens: vi.fn(),
  startAutoRefresh: vi.fn(),
  stopAutoRefresh: vi.fn(),
  getAccessToken: vi.fn(),
  getRefreshTokenValue: vi.fn(),
  clearTokens: vi.fn(),
  isExpired: vi.fn(),
  isAuthFailureFromError: vi.fn(),
  refresh: vi.fn(),
  scrubLegacyTokenStorages: vi.fn(),
  onTokenChanged: vi.fn(),
  loadMirroredAuthSession: vi.fn(),
}))

const growthLoop = vi.hoisted(() => ({
  restartGrowthLoop: vi.fn(),
  destroyGrowthLoop: vi.fn(),
}))

const shellLocalAuth = vi.hoisted(() => ({
  loadShellLocalCredential: vi.fn(async () => null),
  markShellAutoLoginDisabled: vi.fn(),
  shellAutoLoginDisabled: vi.fn(() => false),
  notifyShellLoginOutcome: vi.fn(async () => {}),
}))

vi.mock('@/services/api/auth', () => authApi)
vi.mock('@/services/auth/tokenLifecycle', () => tl)
vi.mock('@/services/authCallbacks', () => ({ registerAuthExpiredCallback: vi.fn() }))
vi.mock('@/services/modules/GrowthLoop', () => growthLoop)
vi.mock('@/services/query/queryClient', () => ({ queryClient: { removeQueries: vi.fn() } }))
vi.mock('@/services/shellLocalAuth', () => shellLocalAuth)

import { useAuthStore } from '@/stores/authStore'

/** 内核限速锁形状（auth.rs TooManyRequests 经拦截器 reject 的附着字段） */
function rateLimitedError(): Error {
  return Object.assign(new Error('登录失败次数过多，请 900 秒后重试'), {
    code: 'TOO_MANY_REQUESTS',
    retry_after_secs: 900,
  })
}

describe('authStore 登录失败横幅三分类', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    tl.getAccessToken.mockReturnValue(null)
    tl.getRefreshTokenValue.mockReturnValue(null)
    tl.isExpired.mockReturnValue(true)
    tl.scrubLegacyTokenStorages.mockImplementation(() => {})
    useAuthStore.setState({ isLoading: false, error: null, isAuthenticated: false })
  })

  it('429 限速锁：横幅=锁定文案（含秒数与重启逃生口，不诱导继续输入）', async () => {
    authApi.login.mockRejectedValue(rateLimitedError())
    await expect(useAuthStore.getState().login('admin', 'password123')).rejects.toThrow()
    expect(useAuthStore.getState().error).toBe('尝试过多已锁定，请等待 900 秒或重启应用')
  })

  it('429：重抛原错误（code 附着保留）——自动链据此识别终态', async () => {
    authApi.login.mockRejectedValue(rateLimitedError())
    const thrown: unknown = await useAuthStore
      .getState()
      .login('admin', 'password123')
      .then(() => null)
      .catch((e: unknown) => e)
    expect((thrown as { code?: string }).code).toBe('TOO_MANY_REQUESTS')
  })

  it('凭据错：横幅=后端原文', async () => {
    authApi.login.mockRejectedValue(
      Object.assign(new Error('用户名或密码错误'), { code: 'BAD_REQUEST' }),
    )
    await expect(useAuthStore.getState().login('admin', 'wrong-password')).rejects.toThrow()
    expect(useAuthStore.getState().error).toBe('用户名或密码错误')
  })

  it('网络失败：横幅=无法连接内核 + 附当前目标基址（残留远程地址可辨别，S5-D①）', async () => {
    localStorage.setItem('server_base_url', 'http://10.0.2.2:9')
    authApi.login.mockRejectedValue(Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' }))
    await expect(useAuthStore.getState().login('admin', 'password123')).rejects.toThrow()
    expect(useAuthStore.getState().error).toBe(
      '无法连接内核（目标：http://10.0.2.2:9），请确认服务已启动或检查服务器地址',
    )
  })
})

describe('authStore 登录结果上报壳层计数面（S5-A③）', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    tl.getAccessToken.mockReturnValue(null)
    tl.getRefreshTokenValue.mockReturnValue(null)
    tl.isExpired.mockReturnValue(true)
    tl.scrubLegacyTokenStorages.mockImplementation(() => {})
    growthLoop.restartGrowthLoop.mockResolvedValue(undefined)
    growthLoop.destroyGrowthLoop.mockImplementation(() => {})
    useAuthStore.setState({ isLoading: false, error: null, isAuthenticated: false })
  })

  it('凭据错：上报 false（壳层看门狗计数面，连续 ≥3 触发重播种）', async () => {
    authApi.login.mockRejectedValue(
      Object.assign(new Error('用户名或密码错误'), { code: 'BAD_REQUEST' }),
    )
    await expect(useAuthStore.getState().login('admin', 'wrong-password')).rejects.toThrow()
    expect(shellLocalAuth.notifyShellLoginOutcome).toHaveBeenCalledWith(false, 'credential')
  })

  it('429 与网络失败：不上报（限速锁不是凭据证据，网络失败无凭据语义）', async () => {
    authApi.login.mockRejectedValue(rateLimitedError())
    await expect(useAuthStore.getState().login('admin', 'password123')).rejects.toThrow()
    authApi.login.mockRejectedValue(
      Object.assign(new Error('Network Error'), { code: 'ERR_NETWORK' }),
    )
    await expect(useAuthStore.getState().login('admin', 'password123')).rejects.toThrow()
    expect(shellLocalAuth.notifyShellLoginOutcome).not.toHaveBeenCalled()
  })

  it('登录成功：上报 true（看门狗闩锁复位=自愈达成证据）', async () => {
    authApi.login.mockResolvedValue({
      access_token: 'at-1',
      refresh_token: 'rt-1',
      expires_in: 3600,
      must_change_password: false,
    })
    authApi.getCurrentUser.mockResolvedValue({
      id: 'u-1',
      username: 'admin',
      email: 'a@b.c',
      role: 'admin',
      is_active: true,
      created_at: '2025-01-01T00:00:00Z',
      last_login_at: null,
    })
    await useAuthStore.getState().login('admin', 'password123')
    expect(shellLocalAuth.notifyShellLoginOutcome).toHaveBeenCalledWith(true)
  })
})
