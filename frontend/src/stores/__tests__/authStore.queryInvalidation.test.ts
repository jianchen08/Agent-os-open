// @feature FP-T12 前端组件补测
/**
 * 认证态翻转必须失效化查询缓存（GUI 黑盒测试 2026-09-11）：
 *
 * 登录前 Sidebar 已挂载的 sessions query 以匿名身份拉取（失败/空），登录成功
 * 后没有任何失效化触发点——列表粘住"暂无会话"，新建会话在空缓存上追加后只
 * 显示 1 条，刷新页面才见到全部会话。契约：login/register 成功 → 查询缓存
 * 全量失效化（匿名期缓存对已认证用户一律不可信）。
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const removeQueries = vi.hoisted(() => vi.fn())

vi.mock('../../services/api/auth', () => ({
  login: vi.fn().mockResolvedValue({
    access_token: 'token-1',
    refresh_token: 'r-1',
    expires_in: 3600,
    must_change_password: false,
  }),
  register: vi.fn().mockResolvedValue({
    access_token: 'token-1',
    refresh_token: 'r-1',
    expires_in: 3600,
    must_change_password: false,
  }),
  getCurrentUser: vi.fn().mockResolvedValue({
    id: 'u-1',
    username: 'admin',
    email: 'a@b.c',
    role: 'admin',
    is_active: true,
    created_at: '2025-01-01T00:00:00Z',
    last_login_at: null,
  }),
}))

vi.mock('../../services/auth/tokenLifecycle', () => ({
  setTokens: vi.fn(),
  startAutoRefresh: vi.fn(),
  stopAutoRefresh: vi.fn(),
  getAccessToken: vi.fn().mockReturnValue('token-1'),
  getRefreshTokenValue: vi.fn().mockReturnValue('r-1'),
  clearTokens: vi.fn(),
  isExpired: vi.fn().mockReturnValue(false),
  isAuthFailureFromError: vi.fn().mockReturnValue(true),
  refresh: vi.fn(),
  scrubLegacyTokenStorages: vi.fn(),
  onTokenChanged: vi.fn().mockReturnValue(vi.fn()),
}))

vi.mock('../../services/query/queryClient', () => ({
  queryClient: { removeQueries },
}))

vi.mock('../../services/modules/GrowthLoop', () => ({
  restartGrowthLoop: vi.fn().mockResolvedValue(undefined),
}))

import { useAuthStore } from '../authStore'

describe('认证态翻转失效化查询缓存', () => {
  beforeEach(() => {
    removeQueries.mockClear()
    localStorage.clear()
    useAuthStore.setState({ token: null, user: null, isAuthenticated: false, error: null })
  })

  it('登录成功后失效化全部查询缓存', async () => {
    await useAuthStore.getState().login('admin', 'pw')
    expect(removeQueries).toHaveBeenCalled()
  })

  it('注册成功后同样失效化', async () => {
    await useAuthStore.getState().register('admin', 'pw', 'a@b.c')
    expect(removeQueries).toHaveBeenCalled()
  })

  it('登录失败（凭据错误）不失效化', async () => {
    const authApi = await import('../../services/api/auth')
    vi.mocked(authApi.login).mockRejectedValueOnce(new Error('用户名或密码错误'))
    await expect(useAuthStore.getState().login('admin', 'wrong')).rejects.toThrow()
    expect(removeQueries).not.toHaveBeenCalled()
  })
})
