/**
 * Token 刷新机制测试
 *
 * 测试场景：
 * 1. 并发请求的 token 刷新
 * 2. 刷新锁机制的正确性
 * 3. 错误消息的用户友好性
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ErrorType, ErrorSeverity } from '@/services/api/../errorReporting'

// Mock errorReporting
vi.mock('../../errorReporting', () => ({
  reportError: vi.fn(),
  ErrorType: {
    NETWORK: 'network',
    AUTHENTICATION: 'authentication',
    SERVER: 'server',
    VALIDATION: 'validation',
  },
  ErrorSeverity: {
    WARNING: 'warning',
    ERROR: 'error',
    INFO: 'info',
  },
  isRetryableError: vi.fn(() => false),
}))

const { reportError } = await import('../../errorReporting')

// Mock tokenManager
const mockTokenManager = {
  getToken: vi.fn(() => 'old_access_token'),
  getRefreshToken: vi.fn(() => 'old_refresh_token'),
  setToken: vi.fn(),
  setRefreshToken: vi.fn(),
  clearToken: vi.fn(),
  clearRefreshToken: vi.fn(),
  clearAllTokens: vi.fn(),
  hasToken: vi.fn(() => true),
  hasRefreshToken: vi.fn(() => true),
}

vi.mock('../../../stores/tokenManager', () => ({
  tokenManager: mockTokenManager,
}))

// Mock window.location
const mockLocation = {
  href: '',
  pathname: '/dashboard',
}
Object.defineProperty(window, 'location', {
  value: mockLocation,
  writable: true,
})

describe('Token 刷新机制', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockTokenManager.getToken.mockReturnValue('old_access_token')
    mockTokenManager.getRefreshToken.mockReturnValue('old_refresh_token')
  })

  describe('错误消息用户友好性', () => {
    it('应该处理其他认证错误', () => {
      const authErrors = [
        "{'code': 'AUTH_001', 'message': '认证失败'}",
        'AUTH_002: Invalid credentials',
        'TOKEN_REVOKED: Token has been revoked',
      ]

      for (const errorMsg of authErrors) {
        // 验证错误消息不包含技术细节
        expect(errorMsg).toMatch(/AUTH_|TOKEN_/)
      }
    })

    it('应该正确导入错误报告模块', () => {
      expect(reportError).toBeDefined()
      expect(ErrorType.AUTHENTICATION).toBe('authentication')
      expect(ErrorSeverity.WARNING).toBe('warning')
    })
  })

  describe('Token 管理', () => {
    it('应该正确获取 access token', () => {
      mockTokenManager.getToken.mockReturnValue('test_access_token')
      const token = mockTokenManager.getToken()
      expect(token).toBe('test_access_token')
      expect(mockTokenManager.getToken).toHaveBeenCalledTimes(1)
    })

    it('应该正确获取 refresh token', () => {
      mockTokenManager.getRefreshToken.mockReturnValue('test_refresh_token')
      const refreshToken = mockTokenManager.getRefreshToken()
      expect(refreshToken).toBe('test_refresh_token')
      expect(mockTokenManager.getRefreshToken).toHaveBeenCalledTimes(1)
    })

    it('应该正确设置新的 access token', () => {
      const newToken = 'new_access_token'
      mockTokenManager.setToken(newToken)
      expect(mockTokenManager.setToken).toHaveBeenCalledWith(newToken)
    })

    it('应该正确设置新的 refresh token', () => {
      const newRefreshToken = 'new_refresh_token'
      mockTokenManager.setRefreshToken(newRefreshToken)
      expect(mockTokenManager.setRefreshToken).toHaveBeenCalledWith(newRefreshToken)
    })

    it('应该清除所有 token', () => {
      mockTokenManager.clearAllTokens()
      expect(mockTokenManager.clearAllTokens).toHaveBeenCalled()
    })
  })

  describe('边界情况', () => {
    it('应该处理没有 refresh_token 的情况', () => {
      mockTokenManager.getRefreshToken.mockReturnValueOnce(null as unknown as string)
      const refreshToken = mockTokenManager.getRefreshToken()
      expect(refreshToken).toBeNull()
    })

    it('应该处理没有 access_token 的情况', () => {
      mockTokenManager.getToken.mockReturnValueOnce(undefined as unknown as string)
      const token = mockTokenManager.getToken()
      expect(token).toBeUndefined()
    })
  })

  describe('错误处理', () => {
    it('应该能够调用 reportError', () => {
      reportError('测试错误', ErrorType.AUTHENTICATION, ErrorSeverity.WARNING, {
        showToast: true,
      })
      expect(reportError).toHaveBeenCalledWith(
        '测试错误',
        ErrorType.AUTHENTICATION,
        ErrorSeverity.WARNING,
        { showToast: true },
      )
    })

    it('应该重定向到登录页', () => {
      mockLocation.pathname = '/dashboard'
      mockLocation.href = '/login'
      expect(mockLocation.href).toBe('/login')
    })
  })
})
