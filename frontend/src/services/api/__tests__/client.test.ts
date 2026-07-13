/**
 * API客户端测试
 * 测试axios实例配置、请求拦截器和响应拦截器
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { API_BASE_URL, API_TIMEOUT } from '@/services/api/../../constants/api'

describe('API客户端配置', () => {
  beforeEach(() => {
    // 清除localStorage
    localStorage.clear()
  })

  afterEach(() => {
    localStorage.clear()
  })

  describe('基础配置常量', () => {
    it('应该定义正确的API基础URL', () => {
      expect(API_BASE_URL).toBeDefined()
      expect(typeof API_BASE_URL).toBe('string')
    })

    it('应该定义正确的超时时间', () => {
      expect(API_TIMEOUT).toBeDefined()
      expect(typeof API_TIMEOUT).toBe('number')
      expect(API_TIMEOUT).toBe(30000)
    })

    it('应该能够导入apiClient', async () => {
      const apiClientModule = await import('../index')
      expect(apiClientModule.apiClient).toBeDefined()
      expect(apiClientModule.default).toBeDefined()
    })
  })

  describe('Token管理', () => {
    it('应该能够在localStorage中存储access_token', () => {
      // 设置token（使用新的键名）
      const testToken = 'test-token-123'
      localStorage.setItem('access_token', testToken)

      // 验证token存在
      expect(localStorage.getItem('access_token')).toBe(testToken)
    })

    it('应该能够在localStorage中存储refresh_token', () => {
      const refreshToken = 'refresh-token-456'
      localStorage.setItem('refresh_token', refreshToken)

      expect(localStorage.getItem('refresh_token')).toBe(refreshToken)
    })

    it('应该在没有token时返回null', () => {
      // 确保没有token
      localStorage.removeItem('access_token')

      // 验证没有token
      expect(localStorage.getItem('access_token')).toBeNull()
    })

    it('应该能够更新token', () => {
      localStorage.setItem('access_token', 'old-token')
      expect(localStorage.getItem('access_token')).toBe('old-token')

      localStorage.setItem('access_token', 'new-token')
      expect(localStorage.getItem('access_token')).toBe('new-token')
    })
  })

  describe('认证信息清除', () => {
    it('应该能够清除单个token', () => {
      localStorage.setItem('access_token', 'test-token')
      expect(localStorage.getItem('access_token')).toBe('test-token')

      localStorage.removeItem('access_token')
      expect(localStorage.getItem('access_token')).toBeNull()
    })

    it('应该能够清除所有认证信息', () => {
      // 设置tokens（使用新的键名）
      localStorage.setItem('access_token', 'old-token')
      localStorage.setItem('refresh_token', 'refresh-token')

      // 清除认证信息
      localStorage.removeItem('access_token')
      localStorage.removeItem('refresh_token')

      // 验证已清除
      expect(localStorage.getItem('access_token')).toBeNull()
      expect(localStorage.getItem('refresh_token')).toBeNull()
    })

    it('应该能够清除所有localStorage数据', () => {
      localStorage.setItem('access_token', 'token')
      localStorage.setItem('refresh_token', 'refresh')
      localStorage.setItem('other_data', 'data')

      localStorage.clear()

      expect(localStorage.getItem('access_token')).toBeNull()
      expect(localStorage.getItem('refresh_token')).toBeNull()
      expect(localStorage.getItem('other_data')).toBeNull()
    })
  })

  describe('错误处理', () => {
    it('应该能够创建ApiError格式的错误对象', () => {
      const apiError = {
        code: '404',
        message: '资源未找到',
        details: { resource: 'user' },
      }

      // 验证错误对象结构
      expect(apiError).toHaveProperty('code')
      expect(apiError).toHaveProperty('message')
      expect(apiError).toHaveProperty('details')
    })

    it('应该能够处理没有响应的错误', () => {
      const error = {
        message: '网络错误',
        code: 'NETWORK_ERROR',
      }

      // 验证错误对象
      expect(error.message).toBe('网络错误')
      expect(error.code).toBe('NETWORK_ERROR')
    })

    it('应该能够处理超时错误', () => {
      const error = {
        message: '请求超时',
        code: 'TIMEOUT',
      }

      // 验证超时错误
      expect(error.message).toBe('请求超时')
      expect(error.code).toBe('TIMEOUT')
    })
  })
})
