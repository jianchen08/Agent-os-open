/**
 * 统一的 Token 管理器
 *
 * 提供统一的 token 访问接口，避免直接访问 localStorage
 * 所有代码应该通过这个管理器来获取/设置 token
 *
 * Requirements: 2.2
 */

import { STORAGE_KEYS } from '../constants/storage'

/**
 * Token 管理器类
 */
class TokenManager {
  /**
   * 获取访问令牌
   * @returns access_token 或 null
   */
  getToken(): string | null {
    try {
      return localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
    } catch (error) {
      console.error('[TokenManager] 获取 access_token 失败:', error)
      return null
    }
  }

  /**
   * 获取刷新令牌
   * @returns refresh_token 或 null
   */
  getRefreshToken(): string | null {
    try {
      return localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)
    } catch (error) {
      console.error('[TokenManager] 获取 refresh_token 失败:', error)
      return null
    }
  }

  /**
   * 设置访问令牌
   * @param token - access_token
   */
  setToken(token: string): void {
    try {
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, token)
    } catch (error) {
      console.error('[TokenManager] 设置 access_token 失败:', error)
    }
  }

  /**
   * 设置刷新令牌
   * @param token - refresh_token
   */
  setRefreshToken(token: string): void {
    try {
      localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, token)
    } catch (error) {
      console.error('[TokenManager] 设置 refresh_token 失败:', error)
    }
  }

  /**
   * 清除访问令牌
   */
  clearToken(): void {
    try {
      localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN)
    } catch (error) {
      console.error('[TokenManager] 清除 access_token 失败:', error)
    }
  }

  /**
   * 清除刷新令牌
   */
  clearRefreshToken(): void {
    try {
      localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
    } catch (error) {
      console.error('[TokenManager] 清除 refresh_token 失败:', error)
    }
  }

  /**
   * 清除所有令牌
   */
  clearAllTokens(): void {
    this.clearToken()
    this.clearRefreshToken()
  }

  /**
   * 检查是否存在访问令牌
   * @returns 是否存在 access_token
   */
  hasToken(): boolean {
    return this.getToken() !== null
  }

  /**
   * 检查是否存在刷新令牌
   * @returns 是否存在 refresh_token
   */
  hasRefreshToken(): boolean {
    return this.getRefreshToken() !== null
  }
}

/**
 * 导出单例实例
 */
export const tokenManager = new TokenManager()

/**
 * 导出类型（用于类型注解）
 */
export type TokenManagerType = TokenManager
