/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * @feature 认证可靠性（token 生命周期单一职责模块） | @ci frontend-test
 *
 * tokenLifecycle 是 token 存取/过期判定/互斥刷新/主动续期调度/认证失效分类的
 * 唯一实现（2026-08-21 架构收口；存储面 2026-09-05 D12-7 立基、2026-09-13
 * 用户裁定「体验优先」修订：access 仅内存、refresh 存 localStorage 跨重启
 * 自动登录、盗用风险由服务端单次轮换/jti 吊销/改密吊销兜底）。本文件覆盖：
 * - AC-8 TTL 边界不变量（isExpired 平移）
 * - 存取唯一入口 + 存储面不变量（access 零落盘、refresh 持久）
 * - 单次轮换（刷新后旧 refresh 被服务端作废，新值必须落位）
 * - 主动续期链退避重试不断链
 * - 无凭据确定性认证失败
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
  changePassword: vi.fn(),
}))
import { STORAGE_KEYS } from '@/constants/storage'
import {
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  clearTokens,
  isExpired,
  isAuthFailureFromError,
  refresh,
  scrubLegacyTokenStorages,
  ensureFreshToken,
  startAutoRefresh,
  stopAutoRefresh,
  loadMirroredAuthSession,
} from '@/services/auth/tokenLifecycle'

const BASE_TIME = new Date('2026-01-01T00:00:00Z').getTime()

describe('tokenLifecycle: TTL 边界不变量 (isExpired)', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('TTL 边界前（刚签发）应未过期 → false', () => {
    setTokens('at-1', 'rt-1', 10)
    expect(isExpired()).toBe(false)
  })

  it('临近边界（9s，TTL 内最后 1s）仍未过期 → false', () => {
    setTokens('at-1', 'rt-1', 10)
    vi.advanceTimersByTime(9_000)
    expect(isExpired()).toBe(false)
  })

  it('越过边界应判定过期 → true', () => {
    setTokens('at-1', 'rt-1', 10)
    vi.advanceTimersByTime(10_001)
    expect(isExpired()).toBe(true)
  })

  it('无过期时间记录（未签发/已清除）视为已过期 → true', () => {
    expect(isExpired()).toBe(true)
    clearTokens()
    expect(isExpired()).toBe(true)
  })
})

describe('tokenLifecycle: 存储面不变量（access 零落盘 / refresh 持久）', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    clearTokens()
  })

  it('access token 仅内存持有：setTokens 后 localStorage 无 access token', () => {
    setTokens('at-1', 'rt-1', 100)
    expect(getAccessToken()).toBe('at-1')
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)).toBeNull()
  })

  it('refresh token 持久存 localStorage（跨浏览器重启自动登录的载体）', () => {
    setTokens('at-1', 'rt-1', 100)
    expect(getRefreshTokenValue()).toBe('rt-1')
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBe('rt-1')
  })

  it('登出全周期：refresh token 从 localStorage 清除且内存清空', () => {
    setTokens('at-1', 'rt-1', 100)
    clearTokens()
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)).toBeNull()
    expect(getAccessToken()).toBeNull()
    expect(getRefreshTokenValue()).toBeNull()
  })

  it('升级残留清擦：access 两键任何版本都非法必清；refresh 键现为合法居所不清', () => {
    // 模拟更早版本写入 localStorage 的令牌三件套 + D12-7 时代的 sessionStorage 残留
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, 'legacy-at')
    localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, 'legacy-rt')
    localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, '9999999999999')
    sessionStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, 'legacy-ss-rt')
    scrubLegacyTokenStorages()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)).toBeNull()
    expect(localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)).toBeNull()
    // refresh token 自 2026-09-13 起持久存 localStorage，scrub 不得清除
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBe('legacy-rt')
    expect(sessionStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
  })

  it('clearTokens 清 localStorage 的 refresh token 并顺带清擦残留（logout 清全侧）', () => {
    setTokens('at-1', 'rt-1', 100)
    sessionStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, 'legacy-ss-rt')
    clearTokens()
    expect(localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
    expect(sessionStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)).toBeNull()
  })
})

describe('tokenLifecycle: 单次轮换（refresh 出新值落位）', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('refresh 成功后新 refresh token 必须落 localStorage（旧值已服务端作废）', async () => {
    setTokens('at-1', 'rt-1', 1)
    vi.advanceTimersByTime(2_000)
    mockApiRefreshToken.mockResolvedValue({
      access_token: 'at-2',
      refresh_token: 'rt-2',
      expires_in: 60,
    })
    await refresh()
    expect(mockApiRefreshToken).toHaveBeenCalledWith('rt-1')
    expect(getRefreshTokenValue()).toBe('rt-2')
    expect(getAccessToken()).toBe('at-2')
  })

  it('刷新响应缺失轮换新值 → 视为失败抛错（不落半会话凭据）', async () => {
    setTokens('at-1', 'rt-1', 1)
    vi.advanceTimersByTime(2_000)
    mockApiRefreshToken.mockResolvedValue({ access_token: 'at-2', expires_in: 60 })
    // 缺失轮换新值按刷新失败处理（外层统一包装为「令牌刷新失败」）
    await expect(refresh()).rejects.toThrow('令牌刷新失败')
  })
})

describe('tokenLifecycle: 无凭据确定性认证失败', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
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
    setTokens('at-1', 'rt-1', 100)
    mockApiRefreshToken.mockRejectedValue(new Error('Network Error'))
    await expect(refresh()).rejects.toThrow('令牌刷新失败')
    expect(mockApiRefreshToken).toHaveBeenCalledWith('rt-1')
  })
})

describe('tokenLifecycle: 存储读故障 ≠ 无凭据（存储故障不呈现为被登出）', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
    mockApiRefreshToken.mockReset()
    vi.useFakeTimers()
    vi.setSystemTime(BASE_TIME)
  })

  afterEach(() => {
    stopAutoRefresh()
    vi.useRealTimers()
  })

  it('读 token 抛异常（存储故障）→ error 日志 + 按瞬时失败上抛（无 authNoCredentials）+ 现态保留', async () => {
    setTokens('at-live', 'rt-live', 100)
    // 异步推进：让定时器触发的在飞 refresh（单飞互斥占位）先落定，
    // 否则下方 refresh() 复用在飞 promise，存储故障分支不会执行
    await vi.advanceTimersByTimeAsync(101_000)
    // 钉实例而非 Storage.prototype：jsdom 下原型级 spy 不保证拦到 localStorage
    // 实例读取；实例自有属性遮蔽原型，任意环境确定性触发存储故障分支
    const getItemSpy = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('localStorage unavailable')
    })
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      let caught: unknown = null
      try {
        await refresh()
        expect.unreachable('存储故障应上抛')
      } catch (e) {
        caught = e
      }
      // 不当作已登出：错误不带 authNoCredentials → 分类为非认证失败
      // （调度器按瞬时故障退避重试，不触发 triggerAuthExpired）
      expect(isAuthFailureFromError(caught)).toBe(false)
      // 存储异常以 error 日志显式呈现
      expect(errorSpy).toHaveBeenCalled()
      // 现态保留：内存 access token 未被清
      expect(getAccessToken()).toBe('at-live')
    } finally {
      getItemSpy.mockRestore()
      errorSpy.mockRestore()
    }
  })

  it('无 token（localStorage 空）→ 保持无凭据确定性失败（登出路径回归）', async () => {
    await expect(refresh()).rejects.toMatchObject({ authNoCredentials: true })
    expect(isAuthFailureFromError(await refresh().catch((e) => e))).toBe(true)
  })
})

describe('tokenLifecycle: 主动续期链退避重试、不断链', () => {
  const TTL_S = 100 // 100s TTL → 首次续期调度在 50s（min(TTL/2, 5min)）

  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
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
    await vi.advanceTimersByTimeAsync((TTL_S * 1000) / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 退避 30s 内不重试
    await vi.advanceTimersByTimeAsync(29_999)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 30s 到点：第 2 次（成功，按新 TTL 重新调度 50s）
    await vi.advanceTimersByTimeAsync(1)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(2)

    // 新 TTL 的续期点触发第 3 次 → 链在持续
    await vi.advanceTimersByTimeAsync((TTL_S * 1000) / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(3)

    // 新 token 已落内存
    expect(getAccessToken()).toBe('at-3')
  })

  it('续期确定性认证失败（401）→ 不重试，链停止', async () => {
    setTokens('at-1', 'rt-1', TTL_S)
    startAutoRefresh()

    mockApiRefreshToken.mockRejectedValue(
      Object.assign(new Error('refresh token invalid'), { response: { status: 401 } }),
    )

    // 到续期点：401 → 认证失败 → 不重试
    await vi.advanceTimersByTimeAsync((TTL_S * 1000) / 2)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)

    // 远超退避窗口后仍只有 1 次调用（链已停，交反应式路径登出）
    await vi.advanceTimersByTimeAsync(10 * 60 * 1000)
    expect(mockApiRefreshToken).toHaveBeenCalledTimes(1)
  })
})

describe('tokenLifecycle: ensureFreshToken（用前保证新鲜）', () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    clearTokens()
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
      access_token: 'at-new',
      refresh_token: 'rt-2',
      expires_in: 60,
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

describe('tokenLifecycle: 主进程镜像回读 (loadMirroredAuthSession)', () => {
  afterEach(() => {
    delete (window as unknown as { electronAPI?: unknown }).electronAPI
  })

  it('镜像持有会话 → 返回 refresh token 原文', async () => {
    ;(window as unknown as { electronAPI: unknown }).electronAPI = {
      authSession: { load: vi.fn().mockResolvedValue('rt-from-mirror') },
    }
    await expect(loadMirroredAuthSession()).resolves.toBe('rt-from-mirror')
  })

  it('镜像返回非字符串（无会话/接口缺席）→ null', async () => {
    ;(window as unknown as { electronAPI: unknown }).electronAPI = {
      authSession: { load: vi.fn().mockResolvedValue(undefined) },
    }
    await expect(loadMirroredAuthSession()).resolves.toBeNull()
  })

  it('镜像读取抛错（桥故障）→ 吞错返回 null 不外泄异常', async () => {
    ;(window as unknown as { electronAPI: unknown }).electronAPI = {
      authSession: { load: vi.fn().mockRejectedValue(new Error('bridge down')) },
    }
    await expect(loadMirroredAuthSession()).resolves.toBeNull()
  })
})
