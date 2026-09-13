/** @feature FP-T12 前端组件补测 | @ci frontend-test */
/**
 * authStore 分支补测（既有 changePassword/queryInvalidation/registerUserInfoFailure
 * 测试之外缺失的生命周期分支）：
 * - 登录态设置：login 成功落态 + must_change_password 两种取值、refresh_token 缺失
 *   回退现有值、获取用户信息失败（非 Error 拒因）不伪造用户
 * - 输入校验：login/register 空凭据拒绝、register 失败上抛
 * - 登出清理：stopAutoRefresh/destroyGrowthLoop/logout API/清令牌/清持久化用户，
 *   API 失败仍清理
 * - initializeAuth：内存 token 恢复（含 storedUser 损坏）、refresh 轮换恢复、
 *   认证失效登出、暂时性故障保留凭据、无凭据、存储异常降级
 * - fetchCurrentUser 映射与首登改密标记、clearError、认证过期回调
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const captured = vi.hoisted(() => ({
  authExpired: null as null | (() => Promise<void>),
}))

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
}))

const growthLoop = vi.hoisted(() => ({
  restartGrowthLoop: vi.fn(),
  destroyGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn(),
  refreshPluginContributions: vi.fn(),
}))

const removeQueries = vi.hoisted(() => vi.fn())

vi.mock('@/services/api/auth', () => authApi)
vi.mock('@/services/auth/tokenLifecycle', () => tl)
vi.mock('@/services/authCallbacks', () => ({
  registerAuthExpiredCallback: (cb: () => Promise<void>) => {
    captured.authExpired = cb
  },
}))
vi.mock('@/services/modules/GrowthLoop', () => growthLoop)
vi.mock('@/services/query/queryClient', () => ({
  queryClient: { removeQueries },
}))

import { STORAGE_KEYS } from '@/constants/storage'
import { useAuthStore } from '@/stores/authStore'

const LOGIN_RESPONSE = {
  access_token: 'at-1',
  refresh_token: 'rt-1',
  expires_in: 3600,
  must_change_password: false,
}

const USER_INFO = {
  id: 'u-1',
  username: 'admin',
  email: 'a@b.c',
  role: 'admin',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  last_login_at: null,
}

describe('authStore 登录态设置', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    // 模块默认桩：未认证、无凭据。逐用例按需覆盖。
    tl.getAccessToken.mockReturnValue(null)
    tl.getRefreshTokenValue.mockReturnValue(null)
    tl.isExpired.mockReturnValue(true)
    tl.isAuthFailureFromError.mockReturnValue(true)
    tl.refresh.mockResolvedValue(undefined)
    tl.scrubLegacyTokenStorages.mockImplementation(() => {})
    growthLoop.restartGrowthLoop.mockResolvedValue(undefined)
    growthLoop.destroyGrowthLoop.mockImplementation(() => {})
    growthLoop.initializeGrowthLoop.mockResolvedValue(undefined)
    growthLoop.refreshPluginContributions.mockResolvedValue(undefined)
    authApi.login.mockResolvedValue({ ...LOGIN_RESPONSE })
    authApi.register.mockResolvedValue({ ...LOGIN_RESPONSE })
    authApi.logout.mockResolvedValue(undefined)
    authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO })
    useAuthStore.setState({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      mustChangePassword: false,
      isLoading: false,
      isInitializing: true,
      error: null,
    })
  })

  it('空用户名或空密码直接拒绝，不触达登录 API', async () => {
    await expect(useAuthStore.getState().login('', 'pw-1234')).rejects.toThrow(
      '用户名和密码不能为空',
    )
    await expect(useAuthStore.getState().login('admin', '')).rejects.toThrow(
      '用户名和密码不能为空',
    )
    expect(authApi.login).not.toHaveBeenCalled()
    expect(useAuthStore.getState().isLoading).toBe(false)
  })

  it.each([
    ['must_change_password=true 置强制改密标记', true],
    ['must_change_password 缺省不置标记', undefined],
  ])('登录成功：%s', async (_label, mustChange) => {
    authApi.login.mockResolvedValue({ ...LOGIN_RESPONSE, must_change_password: mustChange })

    await useAuthStore.getState().login('admin', 'pw-1234')

    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(true)
    expect(s.token).toBe('at-1')
    expect(s.refreshTokenValue).toBe('rt-1')
    expect(s.mustChangePassword).toBe(mustChange === true)
    expect(s.error).toBeNull()
    // 会话收尾：令牌落库 + 主动续期 + 查询缓存清空 + 自生长闭环就绪
    expect(tl.setTokens).toHaveBeenCalledWith('at-1', 'rt-1', 3600)
    expect(tl.startAutoRefresh).toHaveBeenCalled()
    expect(removeQueries).toHaveBeenCalled()
    expect(growthLoop.restartGrowthLoop).toHaveBeenCalled()
    // 用户信息映射（created_at → createdAt）并持久化
    expect(s.user?.createdAt).toBe('2025-01-01T00:00:00Z')
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.AUTH_USER)!)).toMatchObject({
      id: 'u-1',
      username: 'admin',
    })
  })

  it.each([
    ['login', () => useAuthStore.getState().login('admin', 'pw-1234')],
    ['register', () => useAuthStore.getState().register('admin', 'pw-1234', 'a@b.c')],
  ])('%s 后自生长闭环启动失败：仅留痕不阻断登录态', async (_label, act) => {
    growthLoop.restartGrowthLoop.mockRejectedValue(new Error('growth loop down'))
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

    await act()

    // 闭环失败不回滚认证态
    expect(useAuthStore.getState().isAuthenticated).toBe(true)
    expect(errorSpy).toHaveBeenCalled()
    errorSpy.mockRestore()
  })

  it('登录响应缺 refresh_token：setTokens 回退保留现有值', async () => {
    tl.getRefreshTokenValue.mockReturnValue('rt-kept')
    authApi.login.mockResolvedValue({ access_token: 'at-2', expires_in: 1800 })

    await useAuthStore.getState().login('admin', 'pw-1234')

    expect(tl.setTokens).toHaveBeenCalledWith('at-2', 'rt-kept', 1800)
  })

  it('登录成功但获取用户信息失败（非 Error 拒因）：不伪造用户，提示重新登录', async () => {
    authApi.getCurrentUser.mockRejectedValue('socket hang up')

    await useAuthStore.getState().login('admin', 'pw-1234')

    const s = useAuthStore.getState()
    expect(s.user).toBeNull()
    expect(s.error).toContain('登录成功但获取用户信息失败')
    expect(s.error).toContain('请重新登录')
    expect(localStorage.getItem(STORAGE_KEYS.AUTH_USER)).toBeNull()
  })

  it('register 空用户名/空密码/空邮箱分别拒绝，不触达注册 API', async () => {
    await expect(useAuthStore.getState().register('', 'pw-1234', 'a@b.c')).rejects.toThrow(
      '用户名和密码不能为空',
    )
    await expect(useAuthStore.getState().register('u', '', 'a@b.c')).rejects.toThrow(
      '用户名和密码不能为空',
    )
    await expect(useAuthStore.getState().register('u', 'pw-1234', '')).rejects.toThrow(
      '邮箱不能为空',
    )
    expect(authApi.register).not.toHaveBeenCalled()
  })

  it('register API 失败：错误落态并上抛，未进入认证态', async () => {
    authApi.register.mockRejectedValue(new Error('用户名已存在'))

    await expect(useAuthStore.getState().register('u', 'pw-1234', 'a@b.c')).rejects.toThrow(
      '用户名已存在',
    )

    const s = useAuthStore.getState()
    expect(s.error).toBe('用户名已存在')
    expect(s.isLoading).toBe(false)
    expect(s.isAuthenticated).toBe(false)
  })

  it('changePassword 响应缺 refresh_token：保留现有值且清除强制改密标记', async () => {
    tl.getRefreshTokenValue.mockReturnValue('rt-kept')
    authApi.changePassword.mockResolvedValue({ access_token: 'at-new', expires_in: 900 })
    useAuthStore.setState({ mustChangePassword: true, isAuthenticated: true })

    await useAuthStore.getState().changePassword('old-pass', 'new-pass-123')

    expect(tl.setTokens).toHaveBeenCalledWith('at-new', 'rt-kept', 900)
    expect(useAuthStore.getState().refreshTokenValue).toBe('rt-kept')
    expect(useAuthStore.getState().mustChangePassword).toBe(false)
  })
})

describe('authStore 登出清理', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    tl.getRefreshTokenValue.mockReturnValue('rt-1')
    growthLoop.destroyGrowthLoop.mockImplementation(() => {})
    authApi.logout.mockResolvedValue(undefined)
    useAuthStore.setState({
      user: { id: 'u-1', username: 'admin', email: 'a@b.c', role: 'admin', createdAt: '2025-01-01T00:00:00Z' },
      token: 'at-1',
      refreshTokenValue: 'rt-1',
      isAuthenticated: true,
      mustChangePassword: true,
      isLoading: false,
      isInitializing: false,
      error: '旧错误',
    })
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, '{"id":"u-1"}')
  })

  it('登出：停续期、销毁闭环、调用登出 API、清令牌与本地态', async () => {
    await useAuthStore.getState().logout()

    expect(tl.stopAutoRefresh).toHaveBeenCalled()
    expect(growthLoop.destroyGrowthLoop).toHaveBeenCalled()
    expect(authApi.logout).toHaveBeenCalledWith('rt-1')
    expect(tl.clearTokens).toHaveBeenCalled()
    expect(localStorage.getItem(STORAGE_KEYS.AUTH_USER)).toBeNull()

    const s = useAuthStore.getState()
    expect(s.user).toBeNull()
    expect(s.token).toBeNull()
    expect(s.refreshTokenValue).toBeNull()
    expect(s.isAuthenticated).toBe(false)
    expect(s.mustChangePassword).toBe(false)
    expect(s.error).toBeNull()
  })

  it('登出 API 失败仍清除本地认证态', async () => {
    authApi.logout.mockRejectedValue(new Error('网络不可达'))

    await useAuthStore.getState().logout()

    expect(tl.clearTokens).toHaveBeenCalled()
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(useAuthStore.getState().token).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.AUTH_USER)).toBeNull()
  })
})

describe('authStore initializeAuth 恢复分支', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    tl.getAccessToken.mockReturnValue(null)
    tl.getRefreshTokenValue.mockReturnValue(null)
    tl.isExpired.mockReturnValue(true)
    tl.isAuthFailureFromError.mockReturnValue(true)
    tl.refresh.mockResolvedValue(undefined)
    tl.scrubLegacyTokenStorages.mockImplementation(() => {})
    growthLoop.destroyGrowthLoop.mockImplementation(() => {})
    authApi.logout.mockResolvedValue(undefined)
    authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO })
    useAuthStore.setState({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      mustChangePassword: false,
      isLoading: false,
      isInitializing: true,
      error: null,
    })
  })

  it('内存 token 仍有效（同页重建）：直接恢复认证态并异步刷新用户信息', async () => {
    tl.getAccessToken.mockReturnValue('at-mem')
    tl.isExpired.mockReturnValue(false)
    tl.getRefreshTokenValue.mockReturnValue('rt-mem')
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify({ id: 'u-9', username: 'carol' }))

    await useAuthStore.getState().initializeAuth()

    const s = useAuthStore.getState()
    expect(s.isInitializing).toBe(false)
    expect(s.isAuthenticated).toBe(true)
    expect(s.token).toBe('at-mem')
    expect(s.refreshTokenValue).toBe('rt-mem')
    expect(tl.startAutoRefresh).toHaveBeenCalled()
    // 不走 refresh 轮换、不登出
    expect(tl.refresh).not.toHaveBeenCalled()
    expect(tl.clearTokens).not.toHaveBeenCalled()

    // 异步 fetchCurrentUser 落地后以 me 响应为准更新用户信息
    await new Promise((resolve) => setTimeout(resolve, 0))
    expect(authApi.getCurrentUser).toHaveBeenCalled()
    expect(useAuthStore.getState().user?.username).toBe('admin')
  })

  it('storedUser 损坏 JSON：恢复认证态但 user 为 null', async () => {
    // me 接口拒因，隔离 storedUser 解析分支（否则异步 fetchCurrentUser 会回填 user）
    authApi.getCurrentUser.mockRejectedValue(new Error('me endpoint down'))
    tl.getAccessToken.mockReturnValue('at-mem')
    tl.isExpired.mockReturnValue(false)
    tl.getRefreshTokenValue.mockReturnValue('rt-mem')
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, '{broken-json')

    await useAuthStore.getState().initializeAuth()
    // 排空异步 fetchCurrentUser（其失败被静默吞掉）
    await new Promise((resolve) => setTimeout(resolve, 0))

    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(true)
    expect(s.user).toBeNull()
    expect(s.isInitializing).toBe(false)
  })

  it('内存 token 过期：refresh 轮换成功后恢复认证态并回读改密标记', async () => {
    tl.getRefreshTokenValue.mockReturnValue('rt-stored')
    authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO, must_change_password: true })

    await useAuthStore.getState().initializeAuth()

    const s = useAuthStore.getState()
    expect(tl.refresh).toHaveBeenCalled()
    expect(authApi.getCurrentUser).toHaveBeenCalled()
    expect(s.isAuthenticated).toBe(true)
    expect(s.isInitializing).toBe(false)
    expect(s.mustChangePassword).toBe(true)
    expect(tl.clearTokens).not.toHaveBeenCalled()
  })

  it('refresh 失败且判定认证失效：登出清理并保持未认证', async () => {
    tl.getRefreshTokenValue.mockReturnValue('rt-stored')
    tl.refresh.mockRejectedValue(new Error('invalid_grant'))
    tl.isAuthFailureFromError.mockReturnValue(true)

    await useAuthStore.getState().initializeAuth()

    expect(tl.clearTokens).toHaveBeenCalled()
    expect(authApi.logout).toHaveBeenCalled()
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(false)
    expect(s.isInitializing).toBe(false)
  })

  it('refresh 失败但为暂时性故障：保留凭据停留未认证，不登出', async () => {
    tl.getRefreshTokenValue.mockReturnValue('rt-stored')
    tl.refresh.mockRejectedValue(new Error('网关超时'))
    tl.isAuthFailureFromError.mockReturnValue(false)

    await useAuthStore.getState().initializeAuth()

    expect(tl.clearTokens).not.toHaveBeenCalled()
    expect(authApi.logout).not.toHaveBeenCalled()
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(false)
    expect(s.isInitializing).toBe(false)
  })

  it('无任何可恢复凭据：仅结束初始化', async () => {
    await useAuthStore.getState().initializeAuth()

    expect(tl.refresh).not.toHaveBeenCalled()
    expect(useAuthStore.getState().isInitializing).toBe(false)
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
  })

  it('存储不可用（升级清擦抛错）：安全降级结束初始化', async () => {
    tl.scrubLegacyTokenStorages.mockImplementation(() => {
      throw new Error('storage unavailable')
    })

    await useAuthStore.getState().initializeAuth()

    expect(useAuthStore.getState().isInitializing).toBe(false)
    expect(tl.refresh).not.toHaveBeenCalled()
  })
})

describe('authStore 用户信息与过期回调', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    localStorage.clear()
    authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO })
    tl.getAccessToken.mockReturnValue(null)
    tl.getRefreshTokenValue.mockReturnValue(null)
    tl.isExpired.mockReturnValue(true)
    tl.isAuthFailureFromError.mockReturnValue(true)
    tl.refresh.mockResolvedValue(undefined)
    tl.scrubLegacyTokenStorages.mockImplementation(() => {})
    growthLoop.destroyGrowthLoop.mockImplementation(() => {})
    useAuthStore.setState({
      user: null,
      token: 'at-stale',
      refreshTokenValue: 'rt-1',
      isAuthenticated: true,
      mustChangePassword: false,
      isLoading: false,
      isInitializing: false,
      error: '待清除',
    })
  })

  it('fetchCurrentUser：映射用户、持久化 AUTH_USER、回读首登改密标记', async () => {
    authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO, must_change_password: true })

    await useAuthStore.getState().fetchCurrentUser()

    const s = useAuthStore.getState()
    expect(s.user?.id).toBe('u-1')
    expect(s.user?.role).toBe('admin')
    expect(s.mustChangePassword).toBe(true)
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.AUTH_USER)!).id).toBe('u-1')
  })

  it('clearError 清空错误信息', () => {
    expect(useAuthStore.getState().error).toBe('待清除')
    useAuthStore.getState().clearError()
    expect(useAuthStore.getState().error).toBeNull()
  })

  it('认证过期回调：销毁闭环并重置全部认证态', async () => {
    expect(captured.authExpired).toBeTypeOf('function')

    await captured.authExpired!()

    expect(growthLoop.destroyGrowthLoop).toHaveBeenCalled()
    const s = useAuthStore.getState()
    expect(s.user).toBeNull()
    expect(s.token).toBeNull()
    expect(s.refreshTokenValue).toBeNull()
    expect(s.isAuthenticated).toBe(false)
    expect(s.error).toBeNull()
  })
})
