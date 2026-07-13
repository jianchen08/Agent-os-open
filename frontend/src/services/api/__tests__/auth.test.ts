/**
 * 认证API测试
 *
 * 测试login、register、refreshToken、logout接口
 * 与后端 /api/v1/auth/* 端点对齐
 *
 * Requirements: 2.1, 2.5, 2.6
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { login, register, refreshToken, logout, getCurrentUser } from '@/services/api/auth'
// eslint-disable-next-line import-x/order
import type {
  LoginResponse,
  RegisterResponse,
  RefreshResponse,
  LogoutResponse,
  UserInfoResponse,
} from '@/services/api/../../types/api'

// Mock axios
vi.mock('../client', () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
  },
}))

// eslint-disable-next-line import-x/order
import apiClient from '@/services/api/client'

describe('认证API', () => {
  beforeEach(() => {
    // 清除localStorage
    localStorage.clear()
    // 清除所有mock
    vi.clearAllMocks()
  })

  afterEach(() => {
    localStorage.clear()
  })

  describe('login - 登录', () => {
    it('应该成功登录并返回令牌信息', async () => {
      // 准备mock数据（与后端LoginResponse对齐）
      const mockResponse: LoginResponse = {
        access_token: 'test-access-token-123',
        refresh_token: 'test-refresh-token-456',
        token_type: 'bearer',
        expires_in: 3600,
      }

      // Mock axios post方法
      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      // 调用login
      const result = await login('testuser', 'password123')

      // 验证结果
      expect(result).toEqual(mockResponse)
      expect(result.access_token).toBe('test-access-token-123')
      expect(result.refresh_token).toBe('test-refresh-token-456')
      expect(result.token_type).toBe('bearer')
      expect(result.expires_in).toBe(3600)

      // 验证API调用（使用新的端点路径）
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/login', {
        username: 'testuser',
        password: 'password123',
      })
      expect(apiClient.post).toHaveBeenCalledTimes(1)
    })

    it('应该在登录失败时抛出错误', async () => {
      // Mock错误响应
      const mockError = {
        response: {
          status: 401,
          data: {
            detail: '用户名或密码错误',
          },
        },
      }

      vi.mocked(apiClient.post).mockRejectedValueOnce(mockError)

      // 验证抛出错误
      await expect(login('wronguser', 'wrongpass1')).rejects.toThrow()
    })

    it('应该处理网络错误', async () => {
      // Mock网络错误
      const networkError = new Error('Network Error')
      vi.mocked(apiClient.post).mockRejectedValueOnce(networkError)

      // 验证抛出错误
      await expect(login('testuser', 'password1')).rejects.toThrow('Network Error')
    })

    it('应该验证必需参数', async () => {
      // 测试空用户名
      await expect(login('', 'password1')).rejects.toThrow()

      // 测试空密码
      await expect(login('username', '')).rejects.toThrow()
    })
  })

  describe('register - 注册', () => {
    it('应该成功注册并返回token信息', async () => {
      // 准备mock数据（与后端TokenResponse对齐，注册成功后自动登录）
      const mockResponse: RegisterResponse = {
        access_token: 'test-access-token-123',
        refresh_token: 'test-refresh-token-456',
        token_type: 'bearer',
        expires_in: 3600,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 201,
        statusText: 'Created',
        headers: {},
        config: {} as any,
      })

      // 调用register（邮箱现在是必需的）
      const result = await register('newuser', 'password123', 'new@example.com')

      // 验证结果
      expect(result).toEqual(mockResponse)
      expect(result.access_token).toBe('test-access-token-123')
      expect(result.refresh_token).toBe('test-refresh-token-456')
      expect(result.token_type).toBe('bearer')
      expect(result.expires_in).toBe(3600)

      // 验证API调用（使用新的端点路径）
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/register', {
        username: 'newuser',
        password: 'password123',
        email: 'new@example.com',
      })
    })

    it('应该在用户名已存在时抛出错误', async () => {
      const mockError = {
        response: {
          status: 409,
          data: {
            detail: '用户名已存在',
          },
        },
      }

      vi.mocked(apiClient.post).mockRejectedValueOnce(mockError)

      await expect(register('existinguser', 'password1', 'test@example.com')).rejects.toThrow()
    })

    it('应该验证必需参数', async () => {
      // 测试空用户名
      await expect(register('', 'password1', 'test@example.com')).rejects.toThrow()

      // 测试空密码
      await expect(register('username', '', 'test@example.com')).rejects.toThrow()

      // 测试无效邮箱
      await expect(register('username', 'password1', 'invalid-email')).rejects.toThrow()
    })
  })

  describe('refreshToken - 刷新令牌', () => {
    it('应该成功刷新token', async () => {
      // 准备mock数据（与后端RefreshResponse对齐）
      const mockResponse: RefreshResponse = {
        access_token: 'new-access-token',
        refresh_token: 'new-refresh-token',
        token_type: 'bearer',
        expires_in: 3600,
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      // 调用refreshToken
      const result = await refreshToken('old-refresh-token')

      // 验证结果
      expect(result).toEqual(mockResponse)
      expect(result.access_token).toBe('new-access-token')
      expect(result.refresh_token).toBe('new-refresh-token')

      // 验证API调用（使用新的请求格式，refresh 请求显式清除 Authorization 头）
      expect(apiClient.post).toHaveBeenCalledWith(
        '/api/v1/auth/refresh',
        { refresh_token: 'old-refresh-token' },
        { headers: { Authorization: '' } },
      )
    })

    it('应该在refresh token无效时抛出错误', async () => {
      const mockError = {
        response: {
          status: 401,
          data: {
            detail: '刷新令牌已过期，请重新登录',
          },
        },
      }

      vi.mocked(apiClient.post).mockRejectedValueOnce(mockError)

      await expect(refreshToken('invalid-token-12345')).rejects.toThrow()
    })

    it('应该验证必需参数', async () => {
      // 测试空refresh token
      await expect(refreshToken('')).rejects.toThrow()
    })
  })

  describe('logout - 登出', () => {
    it('应该成功登出', async () => {
      // 准备mock数据（与后端LogoutResponse对齐）
      const mockResponse: LogoutResponse = {
        success: true,
        message: '登出成功',
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      // 调用logout
      const result = await logout('refresh-token-123')

      // 验证结果
      expect(result).toEqual(mockResponse)
      expect(result.success).toBe(true)
      expect(result.message).toBe('登出成功')

      // 验证API调用
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/logout', {
        refresh_token: 'refresh-token-123',
        logout_all: false,
      })
    })

    it('应该支持登出所有设备', async () => {
      const mockResponse: LogoutResponse = {
        success: true,
        message: '已登出所有设备',
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      const result = await logout('refresh-token-123', true)

      expect(result.message).toBe('已登出所有设备')
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/logout', {
        refresh_token: 'refresh-token-123',
        logout_all: true,
      })
    })

    it('应该支持不传refresh token的登出', async () => {
      const mockResponse: LogoutResponse = {
        success: true,
        message: '登出成功',
      }

      vi.mocked(apiClient.post).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      const result = await logout()

      expect(result.success).toBe(true)
      expect(apiClient.post).toHaveBeenCalledWith('/api/v1/auth/logout', {
        refresh_token: undefined,
        logout_all: false,
      })
    })
  })

  describe('getCurrentUser - 获取当前用户信息', () => {
    it('应该成功获取当前用户信息', async () => {
      // 准备mock数据（与后端UserResponse对齐）
      const mockResponse: UserInfoResponse = {
        id: '1',
        username: 'testuser',
        email: 'test@example.com',
        role: 'user',
        is_active: true,
        created_at: '2024-01-01T00:00:00Z',
        last_login_at: '2024-01-02T00:00:00Z',
      }

      vi.mocked(apiClient.get).mockResolvedValueOnce({
        data: mockResponse,
        status: 200,
        statusText: 'OK',
        headers: {},
        config: {} as any,
      })

      // 调用getCurrentUser
      const result = await getCurrentUser()

      // 验证结果
      expect(result).toEqual(mockResponse)
      expect(result.username).toBe('testuser')
      expect(result.role).toBe('user')

      // 验证API调用
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/auth/me')
    })
  })

  describe('错误处理和重试逻辑', () => {
    it('应该在网络错误时重试', async () => {
      // 第一次和第二次失败，第三次成功
      const mockResponse: LoginResponse = {
        access_token: 'success-token',
        refresh_token: 'success-refresh',
        token_type: 'bearer',
        expires_in: 3600,
      }

      vi.mocked(apiClient.post)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce({
          data: mockResponse,
          status: 200,
          statusText: 'OK',
          headers: {},
          config: {} as any,
        })

      // 调用login（应该会重试）
      const result = await login('testuser', 'password1', {
        retry: true,
        maxRetries: 3,
      })

      // 验证最终成功
      expect(result.access_token).toBe('success-token')
      // 验证调用了3次
      expect(apiClient.post).toHaveBeenCalledTimes(3)
    })

    it('应该在达到最大重试次数后抛出错误', async () => {
      // 所有尝试都失败
      vi.mocked(apiClient.post).mockRejectedValue(new Error('Network Error'))

      // 验证最终抛出错误
      await expect(login('testuser', 'password1', { retry: true, maxRetries: 2 })).rejects.toThrow(
        'Network Error',
      )

      // 验证调用了2次（初始 + 1次重试）
      expect(apiClient.post).toHaveBeenCalledTimes(2)
    })

    it('应该在服务器错误（5xx）时重试', async () => {
      const mockResponse: LoginResponse = {
        access_token: 'success-token',
        refresh_token: 'success-refresh',
        token_type: 'bearer',
        expires_in: 3600,
      }

      // 第一次500错误，第二次成功
      vi.mocked(apiClient.post)
        .mockRejectedValueOnce({
          response: {
            status: 500,
            data: { message: 'Internal Server Error' },
          },
        })
        .mockResolvedValueOnce({
          data: mockResponse,
          status: 200,
          statusText: 'OK',
          headers: {},
          config: {} as any,
        })

      const result = await login('testuser', 'password1', {
        retry: true,
        maxRetries: 2,
      })

      expect(result.access_token).toBe('success-token')
      expect(apiClient.post).toHaveBeenCalledTimes(2)
    })

    it('应该在客户端错误（4xx）时不重试', async () => {
      // 401错误不应该重试
      vi.mocked(apiClient.post).mockRejectedValue({
        response: {
          status: 401,
          data: {
            detail: '用户名或密码错误',
          },
        },
      })

      await expect(
        login('testuser', 'wrongpass1', { retry: true, maxRetries: 3 }),
      ).rejects.toThrow()

      // 验证只调用了1次，没有重试
      expect(apiClient.post).toHaveBeenCalledTimes(1)
    })
  })

  describe('参数验证', () => {
    it('应该验证登录参数格式', async () => {
      // 用户名太短
      await expect(login('ab', 'password1')).rejects.toThrow()

      // 密码太短（现在要求至少8个字符）
      await expect(login('username', '1234567')).rejects.toThrow()
    })

    it('应该验证注册参数格式', async () => {
      // 用户名太短
      await expect(register('ab', 'password1', 'test@example.com')).rejects.toThrow()

      // 密码太短（现在要求至少8个字符）
      await expect(register('username', '1234567', 'test@example.com')).rejects.toThrow()

      // 邮箱格式错误
      await expect(register('username', 'password1', 'invalid-email')).rejects.toThrow()
    })

    it('应该验证refresh token格式', async () => {
      // token太短
      await expect(refreshToken('abc')).rejects.toThrow()
    })
  })
})
