// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 用户管理 API 服务测试
 *
 * 覆盖 /ext/user_admin/users* 端点封装：用户列表、统计、创建、角色更新、
 * 激活状态更新、删除；失败路径经 reportError 上报后重新抛出。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as usersApi from '@/services/api/users'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import { ErrorSeverity, ErrorType } from '@/services/errorReporting'

const reportErrorMock = vi.fn()
vi.mock('../../errorReporting', () => ({
  reportError: (...args: unknown[]) => reportErrorMock(...args),
  ErrorType: {
    NETWORK: 'network',
    VALIDATION: 'validation',
    AUTHENTICATION: 'authentication',
    AUTHORIZATION: 'authorization',
    NOT_FOUND: 'not_found',
    SERVER: 'server',
    CLIENT: 'client',
    UNKNOWN: 'unknown',
  },
  ErrorSeverity: {
    LOW: 'low',
    MEDIUM: 'medium',
    HIGH: 'high',
    CRITICAL: 'critical',
    INFO: 'info',
    WARNING: 'warning',
    ERROR: 'error',
  },
}))

import apiClient from '@/services/api/client'

const okResponse = (data: unknown) => ({ data })

const user = {
  id: 'u1',
  username: 'alice',
  role: 'user' as const,
  is_active: true,
  created_at: '2026-01-01T00:00:00Z',
}

describe('用户管理 API', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getUsers - 用户列表', () => {
    it('默认参数 skip=0 limit=100', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse([user]))

      const result = await usersApi.getUsers()

      expect(result).toEqual([user])
      expect(apiClient.get).toHaveBeenCalledWith('/ext/user_admin/users', {
        params: { skip: 0, limit: 100 },
      })
    })

    it('自定义分页参数', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse([]))

      await usersApi.getUsers(10, 20)

      expect(apiClient.get).toHaveBeenCalledWith('/ext/user_admin/users', {
        params: { skip: 10, limit: 20 },
      })
    })

    it('失败时上报错误并重新抛出', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('Network Error'))

      await expect(usersApi.getUsers()).rejects.toThrow('Network Error')
      expect(reportErrorMock).toHaveBeenCalledWith(
        '获取用户列表失败',
        {
         type: ErrorType.VALIDATION,
         severity: ErrorSeverity.ERROR,
         code: 'GET_USERS_FAILED'
        },
        )
    })
  })

  describe('getUserStats - 用户统计', () => {
    it('请求统计端点并解包', async () => {
      const stats = { total_users: 3, active_users: 2, admin_count: 1 }
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse(stats))

      const result = await usersApi.getUserStats()

      expect(result.admin_count).toBe(1)
      expect(apiClient.get).toHaveBeenCalledWith('/ext/user_admin/users/stats')
    })

    it('失败时上报错误并重新抛出', async () => {
      vi.mocked(apiClient.get).mockRejectedValueOnce(new Error('Network Error'))

      await expect(usersApi.getUserStats()).rejects.toThrow('Network Error')
      expect(reportErrorMock).toHaveBeenCalledWith(
        '获取用户统计失败',
        {
         type: ErrorType.VALIDATION,
         severity: ErrorSeverity.ERROR,
         code: 'GET_STATS_FAILED'
        },
        )
    })
  })

  describe('createUser - 创建用户', () => {
    it('默认 role=user、email 缺省为空串', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(user))

      const result = await usersApi.createUser({ username: 'alice', password: 'p' })

      expect(result.username).toBe('alice')
      expect(apiClient.post).toHaveBeenCalledWith('/ext/user_admin/users', null, {
        params: { username: 'alice', password: 'p', email: '', role: 'user' },
      })
    })

    it('显式 email 与 role 透传', async () => {
      vi.mocked(apiClient.post).mockResolvedValueOnce(okResponse(user))

      await usersApi.createUser({
        username: 'bob',
        password: 'p',
        email: 'b@x.com',
        role: 'admin',
      })

      expect(apiClient.post).toHaveBeenCalledWith('/ext/user_admin/users', null, {
        params: { username: 'bob', password: 'p', email: 'b@x.com', role: 'admin' },
      })
    })

    it('失败时上报错误并重新抛出', async () => {
      vi.mocked(apiClient.post).mockRejectedValueOnce(new Error('Network Error'))

      await expect(
        usersApi.createUser({ username: 'alice', password: 'p' }),
      ).rejects.toThrow('Network Error')
      expect(reportErrorMock).toHaveBeenCalledWith(
        '创建用户失败',
        {
         type: ErrorType.VALIDATION,
         severity: ErrorSeverity.ERROR,
         code: 'CREATE_USER_FAILED'
        },
        )
    })
  })

  describe('updateUserActiveStatus - 更新激活状态', () => {
    it('PUT 激活状态到用户子路径', async () => {
      vi.mocked(apiClient.put).mockResolvedValueOnce(okResponse({ ...user, is_active: false }))

      const result = await usersApi.updateUserActiveStatus('u1', false)

      expect(result.is_active).toBe(false)
      expect(apiClient.put).toHaveBeenCalledWith('/ext/user_admin/users/u1/active', null, {
        params: { is_active: false },
      })
    })

    it('失败时上报错误并重新抛出', async () => {
      vi.mocked(apiClient.put).mockRejectedValueOnce(new Error('Network Error'))

      await expect(usersApi.updateUserActiveStatus('u1', true)).rejects.toThrow('Network Error')
      expect(reportErrorMock).toHaveBeenCalledWith(
        '更新用户状态失败',
        {
         type: ErrorType.VALIDATION,
         severity: ErrorSeverity.ERROR,
         code: 'UPDATE_STATUS_FAILED'
        },
        )
    })
  })

  describe('deleteUser - 删除用户', () => {
    it('DELETE 用户并解包消息', async () => {
      vi.mocked(apiClient.delete).mockResolvedValueOnce(okResponse({ message: '已删除' }))

      const result = await usersApi.deleteUser('u1')

      expect(result.message).toBe('已删除')
      expect(apiClient.delete).toHaveBeenCalledWith('/ext/user_admin/users/u1')
    })

    it('失败时上报错误并重新抛出', async () => {
      vi.mocked(apiClient.delete).mockRejectedValueOnce(new Error('Network Error'))

      await expect(usersApi.deleteUser('u1')).rejects.toThrow('Network Error')
      expect(reportErrorMock).toHaveBeenCalledWith(
        '删除用户失败',
        {
         type: ErrorType.VALIDATION,
         severity: ErrorSeverity.ERROR,
         code: 'DELETE_USER_FAILED'
        },
        )
    })
  })
})
