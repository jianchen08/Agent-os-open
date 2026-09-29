// @feature: FP-0.2.四 认证链(装机自动登录) | @ci: frontend-test
/**
 * 装机版自动登录 + 改密同步测试（ADR 2026-09-28）
 *
 * - initializeAuth 无任何可恢复凭据时：宿主提供 admin 凭据（仅装机形态非空）
 *   → 走既有 login 链自动认证；宿主为 null（Web/dev）→ 保持未认证；自动登录
 *   失败（内核侧口令不一致等）→ 回落未认证，初始化正常收尾。
 * - changePassword 成功 → 经 electronAPI.adminCredential.sync 回写存档（当前
 *   用户名 + 新口令）；无宿主/回写拒绝/回写异常均不影响改密结果。
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
  initializeGrowthLoop: vi.fn(),
  refreshPluginContributions: vi.fn(),
}))

const removeQueries = vi.hoisted(() => vi.fn())

vi.mock('@/services/api/auth', () => authApi)
vi.mock('@/services/auth/tokenLifecycle', () => tl)
vi.mock('@/services/authCallbacks', () => ({
  registerAuthExpiredCallback: vi.fn(),
}))
vi.mock('@/services/modules/GrowthLoop', () => growthLoop)
vi.mock('@/services/query/queryClient', () => ({
  queryClient: { removeQueries },
}))

import { useAuthStore } from '@/stores/authStore'

const LOGIN_RESPONSE = {
  access_token: 'at-auto',
  refresh_token: 'rt-auto',
  expires_in: 3600,
  must_change_password: false,
}

const USER_INFO = {
  id: 'u-admin',
  username: 'admin',
  email: 'admin@agentos.dev',
  role: 'admin',
  is_active: true,
  created_at: '2025-01-01T00:00:00Z',
  last_login_at: null,
}

type AdminCredentialAPI = {
  load: ReturnType<typeof vi.fn>
  sync: ReturnType<typeof vi.fn>
}

/** 挂载/摘除 window.electronAPI.adminCredential 桩 */
function stubHost(api: AdminCredentialAPI | null): void {
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = api
    ? { adminCredential: api }
    : undefined
}

function hostStubs(): AdminCredentialAPI {
  return { load: vi.fn(), sync: vi.fn() }
}

beforeEach(() => {
  vi.resetAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  stubHost(null)
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
  tl.getAccessToken.mockReturnValue(null)
  tl.getRefreshTokenValue.mockReturnValue(null)
  tl.isExpired.mockReturnValue(true)
  tl.isAuthFailureFromError.mockReturnValue(true)
  tl.refresh.mockResolvedValue(undefined)
  tl.scrubLegacyTokenStorages.mockImplementation(() => {})
  tl.loadMirroredAuthSession.mockResolvedValue(null)
  growthLoop.restartGrowthLoop.mockResolvedValue(undefined)
  growthLoop.destroyGrowthLoop.mockImplementation(() => {})
  authApi.login.mockResolvedValue({ ...LOGIN_RESPONSE })
  authApi.getCurrentUser.mockResolvedValue({ ...USER_INFO })
  authApi.changePassword.mockResolvedValue({ ...LOGIN_RESPONSE })
})

describe('initializeAuth 装机版自动登录分支', () => {
  it('宿主提供凭据 → 以存档账号自动登录成功', async () => {
    const host = hostStubs()
    host.load.mockResolvedValue({ username: 'admin', password: 'seed-pw' })
    stubHost(host)

    await useAuthStore.getState().initializeAuth()

    expect(authApi.login).toHaveBeenCalledWith('admin', 'seed-pw')
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(true)
    expect(s.isInitializing).toBe(false)
    expect(s.user?.username).toBe('admin')
  })

  it('宿主无凭据（Web/dev 形态）→ 不触发登录，保持未认证', async () => {
    const host = hostStubs()
    host.load.mockResolvedValue(null)
    stubHost(host)

    await useAuthStore.getState().initializeAuth()

    expect(authApi.login).not.toHaveBeenCalled()
    expect(useAuthStore.getState().isAuthenticated).toBe(false)
    expect(useAuthStore.getState().isInitializing).toBe(false)
  })

  it('自动登录失败 → 回落未认证，初始化不悬挂', async () => {
    const host = hostStubs()
    host.load.mockResolvedValue({ username: 'admin', password: 'stale-pw' })
    stubHost(host)
    authApi.login.mockRejectedValue(new Error('401 用户名或密码错误'))

    await useAuthStore.getState().initializeAuth()

    expect(authApi.login).toHaveBeenCalledTimes(1)
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(false)
    expect(s.isInitializing).toBe(false)
  })

  it('已有可恢复凭据（refresh token 在）→ 不消费宿主凭据', async () => {
    const host = hostStubs()
    stubHost(host)
    tl.getRefreshTokenValue.mockReturnValue('rt-stored')

    await useAuthStore.getState().initializeAuth()

    expect(host.load).not.toHaveBeenCalled()
    expect(authApi.login).not.toHaveBeenCalled()
  })
})

describe('changePassword 改密后回写凭据存档', () => {
  function seedAuthenticatedUser(username: string): void {
    useAuthStore.setState({
      user: {
        id: 'u-admin',
        username,
        email: 'admin@agentos.dev',
        role: 'admin',
        createdAt: '2025-01-01T00:00:00Z',
      },
      isAuthenticated: true,
    })
  }

  it('admin 改密成功 → sync 收到当前用户名与新口令', async () => {
    seedAuthenticatedUser('admin')
    const host = hostStubs()
    host.sync.mockResolvedValue(true)
    stubHost(host)

    await useAuthStore.getState().changePassword('old-pw', 'new-pw-123')

    expect(authApi.changePassword).toHaveBeenCalledWith('old-pw', 'new-pw-123')
    expect(host.sync).toHaveBeenCalledWith('admin', 'new-pw-123')
    expect(useAuthStore.getState().mustChangePassword).toBe(false)
  })

  it('非 admin 用户改密 → 同样上报（主进程侧拒收非存档账号）', async () => {
    seedAuthenticatedUser('alice')
    const host = hostStubs()
    host.sync.mockResolvedValue(false)
    stubHost(host)

    await useAuthStore.getState().changePassword('old-pw', 'new-pw-456')

    expect(host.sync).toHaveBeenCalledWith('alice', 'new-pw-456')
  })

  it('无宿主（Web 形态）→ 改密正常完成，不报错', async () => {
    seedAuthenticatedUser('admin')
    stubHost(null)

    await expect(
      useAuthStore.getState().changePassword('old-pw', 'new-pw-789'),
    ).resolves.toBeUndefined()
    expect(useAuthStore.getState().mustChangePassword).toBe(false)
  })

  it('sync 异常 → 吞掉不失败改密（回写尽力而为）', async () => {
    seedAuthenticatedUser('admin')
    const host = hostStubs()
    host.sync.mockRejectedValue(new Error('ipc boom'))
    stubHost(host)

    await expect(
      useAuthStore.getState().changePassword('old-pw', 'new-pw-abc'),
    ).resolves.toBeUndefined()
  })
})
