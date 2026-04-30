/**
 * 认证状态管理Store
 *
 * 使用真实后端API进行认证操作。
 * Requirements: 2.1, 2.5, 2.6
 */

import { create } from 'zustand'
import { STORAGE_KEYS } from '../constants/storage'
import * as authApi from '../services/api/auth'
import type { LoginResponse, RefreshResponse, UserInfoResponse } from '../types/api'
import type { User } from '../types/models'

/**
 * 认证状态接口
 */
interface AuthState {
  /** 当前用户 */
  user: User | null
  /** 访问令牌 */
  token: string | null
  /** 刷新令牌 */
  refreshTokenValue: string | null
  /** 是否已认证 */
  isAuthenticated: boolean
  /** 是否正在加载 */
  isLoading: boolean
  /** 是否正在初始化认证状态 */
  isInitializing: boolean
  /** 错误信息 */
  error: string | null
  /** 登录 */
  login: (username: string, password: string) => Promise<void>
  /** 注册 */
  register: (username: string, password: string, email: string) => Promise<void>
  /** 登出 */
  logout: () => Promise<void>
  /** 刷新令牌 */
  refreshToken: () => Promise<void>
  /** 初始化认证状态（从localStorage恢复） */
  initializeAuth: () => Promise<void>
  /** 检查token是否过期 */
  checkTokenExpiration: () => boolean
  /** 获取当前用户信息 */
  fetchCurrentUser: () => Promise<void>
  /** 清除错误 */
  clearError: () => void
}

/**
 * 将后端用户信息响应映射为前端User模型
 */
function mapUserInfoToUser(userInfo: UserInfoResponse): User {
  return {
    id: userInfo.id,
    username: userInfo.username,
    email: userInfo.email,
    createdAt: userInfo.created_at,
  }
}

/**
 * 认证Store
 *
 * 使用真实后端API进行认证操作。
 * Requirements: 2.1, 2.5, 2.6
 */
export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: null,
  refreshTokenValue: null,
  isAuthenticated: false,
  isLoading: false,
  isInitializing: true, // 初始状态为true，表示正在初始化
  error: null,

  /**
   * 登录
   *
   * 调用后端 POST /api/v1/auth/login 端点进行认证。
   * 成功后存储access_token和refresh_token到localStorage。
   *
   * Requirements: 2.1, 2.2
   */
  login: async (username: string, password: string) => {
    // 验证输入
    if (!username || !password) {
      throw new Error('用户名和密码不能为空')
    }

    set({ isLoading: true, error: null })

    try {
      // 调用真实API进行登录
      // Requirements: 2.1
      const response: LoginResponse = await authApi.login(username, password)

      // 计算token过期时间（基于后端返回的expires_in）
      const expiryTime = Date.now() + response.expires_in * 1000

      // 持久化到localStorage（使用 STORAGE_KEYS 常量）
      // Requirements: 2.2
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, response.access_token)
      localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, response.refresh_token)
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, expiryTime.toString())

      // 更新状态（先设置token，以便后续API调用可以使用）
      set({
        token: response.access_token,
        refreshTokenValue: response.refresh_token,
        isAuthenticated: true,
        isLoading: false,
        error: null,
      })

      // 获取用户信息
      try {
        const userInfo = await authApi.getCurrentUser()
        const user = mapUserInfoToUser(userInfo)

        // 持久化用户信息
        localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))

        set({ user })
      } catch (_userError) {
        // 获取用户信息失败，但登录已成功，使用基本用户信息
        const basicUser: User = {
          id: 'unknown',
          username,
          createdAt: new Date().toISOString(),
        }
        localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(basicUser))
        set({ user: basicUser })
      }
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '登录失败'
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /**
   * 注册
   *
   * 调用后端 POST /api/v1/auth/register 端点创建账户。
   * 注册成功后自动登录，获取并存储token。
   *
   * Requirements: 2.5
   */
  register: async (username: string, password: string, email: string) => {
    // 验证输入
    if (!username || !password) {
      throw new Error('用户名和密码不能为空')
    }
    if (!email) {
      throw new Error('邮箱不能为空')
    }

    set({ isLoading: true, error: null })

    try {
      // 调用真实API进行注册
      // 后端注册成功后自动返回token，实现注册即登录
      // Requirements: 2.5
      const response = await authApi.register(username, password, email)

      // 计算token过期时间（基于后端返回的expires_in）
      const expiryTime = Date.now() + response.expires_in * 1000

      // 持久化到localStorage（使用 STORAGE_KEYS 常量）
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, response.access_token)
      localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, response.refresh_token)
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, expiryTime.toString())

      // 更新状态（先设置token，以便后续API调用可以使用）
      set({
        token: response.access_token,
        refreshTokenValue: response.refresh_token,
        isAuthenticated: true,
        isLoading: false,
        error: null,
      })

      // 获取用户信息
      try {
        const userInfo = await authApi.getCurrentUser()
        const user = mapUserInfoToUser(userInfo)

        // 持久化用户信息
        localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))

        set({ user })
      } catch (_userError) {
        // 获取用户信息失败，但注册/登录已成功，使用基本用户信息
        const basicUser: User = {
          id: 'unknown',
          username,
          createdAt: new Date().toISOString(),
        }
        localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(basicUser))
        set({ user: basicUser })
      }
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '注册失败'
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /**
   * 登出
   *
   * 调用后端 POST /api/v1/auth/logout 端点并清除本地令牌。
   *
   * Requirements: 2.6
   */
  logout: async () => {
    const refreshTokenValue =
      get().refreshTokenValue || localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)

    try {
      // 调用后端登出API
      // Requirements: 2.6
      if (refreshTokenValue) {
        await authApi.logout(refreshTokenValue)
      }
    } catch (_error) {
      // 登出API调用失败，仍然清除本地状态
    }

    // 清除localStorage（使用 STORAGE_KEYS 常量）
    localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN)
    localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
    localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
    localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)

    // 清除状态
    set({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      error: null,
    })
  },

  /**
   * 刷新令牌
   *
   * 调用后端 POST /api/v1/auth/refresh 端点刷新访问令牌。
   *
   * Requirements: 2.3
   */
  refreshToken: async () => {
    const currentRefreshToken =
      get().refreshTokenValue || localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)

    if (!currentRefreshToken) {
      throw new Error('没有可刷新的令牌')
    }

    try {
      // 调用真实API刷新令牌
      const response: RefreshResponse = await authApi.refreshToken(currentRefreshToken)

      // 计算新的token过期时间
      const expiryTime = Date.now() + response.expires_in * 1000

      // 持久化到localStorage（使用 STORAGE_KEYS 常量）
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, response.access_token)
      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, expiryTime.toString())

      // 如果返回了新的refresh_token，也更新它
      if (response.refresh_token) {
        localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, response.refresh_token)
      }

      // 更新状态
      set({
        token: response.access_token,
        refreshTokenValue: response.refresh_token || currentRefreshToken,
      })
    } catch (_error: unknown) {
      // 刷新失败，清除认证状态
      await get().logout()
      throw new Error('令牌刷新失败，请重新登录')
    }
  },

  /**
   * 初始化认证状态（从localStorage恢复）
   *
   * 如果存储的token有效，恢复认证状态并获取最新用户信息。
   */
  initializeAuth: async () => {
    try {
      const storedToken = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
      const storedRefreshToken = localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)
      const storedUser = localStorage.getItem(STORAGE_KEYS.AUTH_USER)
      const storedExpiry = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)

      if (storedToken && storedExpiry) {
        // 检查token是否过期
        const expiryTime = parseInt(storedExpiry, 10)

        if (isNaN(expiryTime)) {
          // 过期时间格式错误，清除所有数据
          await get().logout()
          set({ isInitializing: false })
          return
        }

        const isExpired = Date.now() > expiryTime

        if (isExpired) {
          // Token已过期，尝试刷新
          if (storedRefreshToken) {
            try {
              set({
                refreshTokenValue: storedRefreshToken,
                token: storedToken, // 临时设置，以便刷新API可以工作
              })
              await get().refreshToken()

              // 刷新成功，获取用户信息
              await get().fetchCurrentUser()
              set({ isInitializing: false })
              return
            } catch (_refreshError) {
              await get().logout()
              set({ isInitializing: false })
              return
            }
          } else {
            // 没有refresh_token，清除所有数据
            await get().logout()
            set({ isInitializing: false })
            return
          }
        }

        // Token未过期，恢复认证状态
        let user: User | null = null
        if (storedUser) {
          try {
            user = JSON.parse(storedUser) as User
          } catch (_parseError) {
            // 解析失败，使用 null
          }
        }

        set({
          user,
          token: storedToken,
          refreshTokenValue: storedRefreshToken,
          isAuthenticated: true,
          isInitializing: false,
        })

        // 异步获取最新用户信息
        get()
          .fetchCurrentUser()
          .catch(() => {
            // 获取失败，静默处理
          })
      } else {
        // 没有存储的token，初始化完成
        set({ isInitializing: false })
      }
    } catch (_error) {
      // localStorage不可用或其他错误，安全降级
      set({ isInitializing: false })
    }
  },

  /**
   * 检查token是否过期
   * @returns true表示已过期，false表示未过期
   */
  checkTokenExpiration: () => {
    try {
      const storedExpiry = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)

      if (!storedExpiry) {
        // 没有过期时间记录，视为已过期
        return true
      }

      const expiryTime = parseInt(storedExpiry, 10)

      if (isNaN(expiryTime)) {
        // 过期时间格式错误，视为已过期
        return true
      }

      const isExpired = Date.now() > expiryTime
      return isExpired
    } catch (_error) {
      // 发生错误，视为已过期
      return true
    }
  },

  /**
   * 获取当前用户信息
   *
   * 调用后端 GET /api/v1/auth/me 端点获取用户信息。
   */
  fetchCurrentUser: async () => {
    try {
      const userInfo = await authApi.getCurrentUser()
      const user = mapUserInfoToUser(userInfo)

      // 持久化用户信息（使用 STORAGE_KEYS 常量）
      localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))

      set({ user })
    } catch (error: unknown) {
      throw error
    }
  },

  /**
   * 清除错误
   */
  clearError: () => {
    set({ error: null })
  },
}))
