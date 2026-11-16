// @feature: FP-0.2.四 认证链(装机自动登录) | @ci: frontend-test
/**
 * 装机版自动登录测试（ADR 2026-10-02-packaged-auto-login-env-only）
 *
 * - initializeAuth 无任何可恢复凭据时：宿主提供 admin 凭据（仅装机形态且
 *   launcher 已播种 AGENTOS_ADMIN_PASSWORD 时非空）→ 走既有 login 链自动
 *   认证；宿主为 null（Web/dev/未播种）→ 保持未认证；自动登录失败（内核侧
 *   口令不一致等）→ 回落未认证，初始化正常收尾。
 * - env 即口令事实源：无凭据存档、无改密回写通道（改密回写语义已随存档
 *   一并移除，内核「env 不符即重置」承担对齐）。
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

// 借权面（启动即借权路径用）：本机内核代持令牌向目标换发 + 壳层配对设备直借
const borrow = vi.hoisted(() => ({
  mintLocalKernelToken: vi.fn(async () => 'local-at'),
  borrowSession: vi.fn(async () => ({ ok: true, refreshToken: 'rt-borrow' })),
  borrowViaShellDevice: vi.fn(async () => ({ ok: false, error: 'unset' })),
}))

vi.mock('@/services/deviceBorrow', () => ({
  mintLocalKernelToken: (...args: unknown[]) => borrow.mintLocalKernelToken(...args),
  borrowSession: (...args: unknown[]) => borrow.borrowSession(...args),
  borrowViaShellDevice: (...args: unknown[]) => borrow.borrowViaShellDevice(...args),
  borrowAndConnect: vi.fn(),
}))
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
}

/** 挂载/摘除 window.electronAPI.adminCredential 桩 */
function stubHost(api: AdminCredentialAPI | null): void {
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = api
    ? { adminCredential: api }
    : undefined
}

function hostStubs(): AdminCredentialAPI {
  return { load: vi.fn() }
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
  borrow.mintLocalKernelToken.mockResolvedValue(null)
  borrow.borrowSession.mockResolvedValue({ ok: false, error: 'unset' })
  borrow.borrowViaShellDevice.mockResolvedValue({ ok: false, error: 'unset' })
})

describe('initializeAuth 装机版自动登录分支', () => {
  it('宿主提供凭据 → 以 env 事实源口令自动登录成功', async () => {
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

  it('宿主无凭据（Web/dev/未播种 env）→ 不触发登录，保持未认证', async () => {
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

  it('存量 refresh 认证失效 → 清凭据后继续装机免填自动登录（不短路）', async () => {
    // 2026-10-22 用户实测根因：坏 token 走 logout 后 return，短路下方自动
    // 登录链——「无密码切回本机」劣化为每次手输。契约：认证失效清凭据后
    // 落入宿主凭据链，静默恢复认证。
    const host = hostStubs()
    host.load.mockResolvedValue({ username: 'admin', password: 'seed-pw' })
    stubHost(host)
    tl.getRefreshTokenValue.mockReturnValue('rt-stale')
    tl.refresh.mockRejectedValue(new Error('401 令牌失效'))

    await useAuthStore.getState().initializeAuth()

    expect(tl.clearTokens).toHaveBeenCalled()
    expect(authApi.login).toHaveBeenCalledWith('admin', 'seed-pw')
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(true)
    expect(s.isInitializing).toBe(false)
  })

  it('自定义后端档（server_base_url 已设）→ 本机凭据只用于本机借权，不打远端', async () => {
    // 方向铁律：远端档下本机凭据只与**本机内核**交互（借权），绝不作为远端
    // 账号口令提交（凭据外泄面 + 触发对方限速锁）。借权不可用才落未认证。
    const host = hostStubs()
    host.load.mockResolvedValue({ username: 'admin', password: 'seed-pw' })
    stubHost(host)
    localStorage.setItem('server_base_url', 'http://192.168.1.50:9101')
    borrow.mintLocalKernelToken.mockResolvedValue('local-at')
    borrow.borrowSession.mockResolvedValue({ ok: false, error: '借权不可用' })

    await useAuthStore.getState().initializeAuth()

    expect(authApi.login).not.toHaveBeenCalled()
    // 本机凭据只走本机借权面（deviceBorrow 内部取凭据→打本机），不打远端
    expect(borrow.mintLocalKernelToken).toHaveBeenCalled()
    expect(borrow.borrowSession).toHaveBeenCalledWith('http://192.168.1.50:9101', 'local-at')
    const s = useAuthStore.getState()
    expect(s.isAuthenticated).toBe(false)
    expect(s.isInitializing).toBe(false)
  })
})

// 壳本机内核档凭据路径（shellLocalAuth）——文件顶部无既有 mock，补挂默认
// null 保既有用例语义不变；成功路径为本组新增覆盖面。
const shellAuth = vi.hoisted(() => ({
  loadShellLocalCredential: vi.fn(),
  shellAutoLoginDisabled: vi.fn((): boolean => false),
}))

vi.mock('@/services/shellLocalAuth', () => {
  return {
    loadShellLocalCredential: (...args: unknown[]) => shellAuth.loadShellLocalCredential(...args),
    shellAutoLoginDisabled: (...args: unknown[]) => shellAuth.shellAutoLoginDisabled(...args),
    markShellAutoLoginDisabled: vi.fn(),
    notifyShellLoginOutcome: vi.fn().mockResolvedValue(undefined),
    __shellAuth: shellAuth,
  }
})

describe('initializeAuth 壳本机内核档凭据（移动壳自动登录）', () => {
  it('壳凭据就绪且未禁用 → 直接登录成功，初始化收口', async () => {
    const { __shellAuth } = await import('@/services/shellLocalAuth') as unknown as {
      __shellAuth: { loadShellLocalCredential: ReturnType<typeof vi.fn>; shellAutoLoginDisabled: ReturnType<typeof vi.fn> }
    }
    __shellAuth.shellAutoLoginDisabled.mockReturnValue(false)
    __shellAuth.loadShellLocalCredential.mockResolvedValue({ username: 'admin', password: 'device-pw' })
    try {
      await useAuthStore.getState().initializeAuth()
      expect(authApi.login).toHaveBeenCalledWith('admin', 'device-pw')
      expect(useAuthStore.getState().isInitializing).toBe(false)
      expect(useAuthStore.getState().isAuthenticated).toBe(true)
    } finally {
      __shellAuth.loadShellLocalCredential.mockReset()
      __shellAuth.loadShellLocalCredential.mockResolvedValue(null)
      __shellAuth.shellAutoLoginDisabled.mockReturnValue(false)
    }
  })
})

describe('initializeAuth 启动即借权（远端档免登）', () => {
  it('自定义后端 + 无存量令牌 → 借权换发并自动恢复认证（零登录面）', async () => {
    // 用户 2026-10-22 实测：应用启动即指向远端且无存量令牌时落登录闸——
    // 契约：先借权（本机代持令牌）再常规 refresh，全程不要求手输。
    localStorage.setItem('server_base_url', 'http://162.14.69.153')
    borrow.mintLocalKernelToken.mockResolvedValue('local-at')
    borrow.borrowSession.mockResolvedValue({ ok: true, refreshToken: 'rt-borrow' })

    await useAuthStore.getState().initializeAuth()

    expect(borrow.borrowSession).toHaveBeenCalledWith('http://162.14.69.153', 'local-at')
    expect(localStorage.getItem('refresh_token@http://162.14.69.153')).toBe('rt-borrow')
    const st = useAuthStore.getState()
    expect(st.isAuthenticated).toBe(true)
    expect(st.isInitializing).toBe(false)
  })

  it('移动壳远端档冷启动：本机栈不拉起（mint null）→ 壳配对设备直借恢复认证', async () => {
    // 用户 2026-10-26 实测：移动端连云端仍落登录页——远端档本机 proot 栈按
    // 设计不拉起，本机代持腿必然 mint 失败；契约：壳层 per-device token 直借
    // 云端 relay /borrow_auth 补位，全程零登录面。
    localStorage.setItem('server_base_url', 'http://162.14.69.153')
    borrow.mintLocalKernelToken.mockResolvedValue(null)
    borrow.borrowViaShellDevice.mockResolvedValue({ ok: true, refreshToken: 'rt-shell' })

    await useAuthStore.getState().initializeAuth()

    expect(borrow.borrowViaShellDevice).toHaveBeenCalledWith('http://162.14.69.153')
    expect(localStorage.getItem('refresh_token@http://162.14.69.153')).toBe('rt-shell')
    expect(borrow.borrowSession).not.toHaveBeenCalled()
    const st = useAuthStore.getState()
    expect(st.isAuthenticated).toBe(true)
    expect(st.isInitializing).toBe(false)
  })

  it('本机腿握手失败 → 壳层直借腿补位（先成先得）', async () => {
    localStorage.setItem('server_base_url', 'http://162.14.69.153')
    borrow.mintLocalKernelToken.mockResolvedValue('local-at')
    borrow.borrowSession.mockResolvedValue({ ok: false, error: '借权不可用' })
    borrow.borrowViaShellDevice.mockResolvedValue({ ok: true, refreshToken: 'rt-shell' })

    await useAuthStore.getState().initializeAuth()

    expect(borrow.borrowSession).toHaveBeenCalledTimes(1)
    expect(borrow.borrowViaShellDevice).toHaveBeenCalledWith('http://162.14.69.153')
    expect(localStorage.getItem('refresh_token@http://162.14.69.153')).toBe('rt-shell')
    expect(useAuthStore.getState().isAuthenticated).toBe(true)
  })
})

describe('initializeAuth 存量令牌失效后的重借（远端档）', () => {
  it('存量 refresh 认证失败 → 重新借权并恢复认证（零登录面）', async () => {
    // 用户 2026-10-22 二次撞墙：目标内核重启致令牌作废，启动链只清凭据不重借
    // → 又落登录页。契约：失效即重借并重试 refresh。
    localStorage.setItem('server_base_url', 'http://162.14.69.153')
    tl.getRefreshTokenValue.mockReturnValue('rt-stale')
    tl.refresh
      .mockRejectedValueOnce(new Error('401 令牌失效'))
      .mockResolvedValueOnce(undefined)
    borrow.mintLocalKernelToken.mockResolvedValue('local-at')
    borrow.borrowSession.mockResolvedValue({ ok: true, refreshToken: 'rt-fresh' })

    await useAuthStore.getState().initializeAuth()

    expect(borrow.borrowSession).toHaveBeenCalledWith('http://162.14.69.153', 'local-at')
    expect(localStorage.getItem('refresh_token@http://162.14.69.153')).toBe('rt-fresh')
    const st = useAuthStore.getState()
    expect(st.isAuthenticated).toBe(true)
    expect(st.isInitializing).toBe(false)
  })
})
