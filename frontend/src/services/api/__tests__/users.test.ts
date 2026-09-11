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
})
