/** 认证状态管理Store 使用真实后端API进行认证操作。 */

import { create } from 'zustand'
import { STORAGE_KEYS } from '../constants/storage'
import * as authApi from '../services/api/auth'
import { registerAuthExpiredCallback } from '../services/authCallbacks'
import type { LoginResponse, RefreshResponse, UserInfoResponse } from '../types/api'
import type { User } from '../types/models'

/** 判断错误是否为「真正认证失效」（应触发 logout） */
export function isAuthFailureFromError(error: unknown): boolean {
  if (!error) return false
  // 直接的 axios 错误
  const directStatus = (error as { response?: { status?: number } })?.response?.status
  if (directStatus === 401 || directStatus === 403) return true
  // 被 refreshToken 包装的错误（Error with cause）
  const cause = (error as { cause?: unknown })?.cause
  const causeStatus = (cause as { response?: { status?: number } })?.response?.status
  if (causeStatus === 401 || causeStatus === 403) return true
  return false
}

/** 认证状态接口 */
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

/** 令牌刷新互斥锁（in-flight Promise） */
let refreshInFlight: Promise<void> | null = null

/** 主动刷新定时器（过期前续期，避免 WS 因 token 过期断连） */
let tokenRefreshTimer: ReturnType<typeof setTimeout> | null = null

/** 提前刷新的最大余量（毫秒），避免 TTL 很大时刷新过于提前 */
const TOKEN_REFRESH_MAX_LEAD_MS = 5 * 60 * 1000

/**
 * 安排下一次主动刷新：在过期前 min(剩余寿命 / 2, 5分钟) 时刷新。
 * 每次 login/register/refresh 成功后调用，setTimeout 单次 + 成功后重新调度，
 * 精确跟随实际 expires_in（每次刷新后 TTL 会变）。失败按 refreshToken 错误处理。
 *
 * 暂停：该定时器疑似导致"十几分钟后被登出"。后端用 refresh token 轮换
 * （每次 refresh 撤销旧 refresh_token），定时器在 ~12.5min 触发的 refresh 可能
 * 与其它路径竞争，用了已撤销的 token → 401 → 登出。先停用定位，确认后再设计
 * 安全的续期方案。access token 30min 内不会过期，停用期间不影响正常使用。
 */
function scheduleTokenRefresh(): void {
  // 暂停主动刷新：见上方函数注释。保留实现便于后续修复后恢复。
  return
  if (tokenRefreshTimer) {
    clearTimeout(tokenRefreshTimer)
    tokenRefreshTimer = null
  }
  const storedExpiry = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)
  if (!storedExpiry) return
  const expiryTime = parseInt(storedExpiry, 10)
  if (isNaN(expiryTime)) return
  const remainingMs = expiryTime - Date.now()
  if (remainingMs <= 0) return // 已过期，由反应式路径处理
  // 提前量：剩余寿命的一半，但不超过 5 分钟。TTL<10min 时取一半避免短 TTL 测试环境刷太频。
  const delay = Math.max(1000, Math.min(remainingMs / 2, TOKEN_REFRESH_MAX_LEAD_MS))
  tokenRefreshTimer = setTimeout(() => {
    useAuthStore
      .getState()
      .refreshToken()
      .then(() => scheduleTokenRefresh()) // 刷新成功（新的 expires_in）后重新调度
      .catch(() => {
        // 刷新失败：由 refreshToken 错误处理决策，清 timer 不再主动续期。
        // 反应式路径（401/WS 4001）仍可兜底恢复。
        tokenRefreshTimer = null
      })
  }, delay)
}

/** 清除主动刷新定时器（登出时调用） */
function clearTokenRefreshTimer(): void {
  if (tokenRefreshTimer) {
    clearTimeout(tokenRefreshTimer)
    tokenRefreshTimer = null
  }
}

/** 将后端用户信息响应映射为前端User模型 */
function mapUserInfoToUser(userInfo: UserInfoResponse): User {
  return {
    id: userInfo.id,
    username: userInfo.username,
    email: userInfo.email,
    createdAt: userInfo.created_at,
  }
}

/** 认证Store 使用真实后端API进行认证操作。 */
export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  token: null,
  refreshTokenValue: null,
  isAuthenticated: false,
  isLoading: false,
  isInitializing: true, // 初始状态为true，表示正在初始化
  error: null,

  /** 登录 调用后端 POST /api/v1/auth/login 端点进行认证。 */
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
        localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
        const userError = _userError instanceof Error ? _userError.message : '获取用户信息失败'
        set({
          user: null,
          error: `登录成功但获取用户信息失败：${userError}，请重新登录`,
        })
      }

      // 安排主动刷新：token 过期前续期，避免 WS 因 token 过期反复断连
      scheduleTokenRefresh()

 // 登录成功后 await restartGrowthLoop 确保模块就绪
      try {
        const { restartGrowthLoop } = await import('@/services/modules/GrowthLoop')
        await restartGrowthLoop()
      } catch (err) {
        console.error('登录后启动自生长闭环失败:', err)
      }
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '登录失败'
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /** 注册 调用后端 POST /api/v1/auth/register 端点创建账户。 */
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

      // 安排主动刷新：token 过期前续期，避免 WS 因 token 过期反复断连
      scheduleTokenRefresh()

 // 注册成功后 await restartGrowthLoop 确保模块就绪
      try {
        const { restartGrowthLoop } = await import('@/services/modules/GrowthLoop')
        await restartGrowthLoop()
      } catch (err) {
        console.error('注册后启动自生长闭环失败:', err)
      }
    } catch (error: unknown) {
      // 兼容 Error 实例与 ApiError 对象（{code,message,details}）：
      // axios 拦截器 reject 的是 ApiError plain object，不是 Error 实例，
      // 否则后端返回的 detail 文案（如"未开启公开注册"）会被吞成通用"注册失败"。
      const errorMessage =
        (error && typeof error === 'object' && 'message' in error
          ? String((error as { message?: unknown }).message)
          : error instanceof Error
            ? error.message
            : '') || '注册失败'
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /** 登出 调用后端 POST /api/v1/auth/logout 端点并清除本地令牌。 */
  logout: async () => {
 // 登出时 await destroyGrowthLoop 确保完全清理
    // 清除主动刷新定时器
    clearTokenRefreshTimer()
    try {
      const { destroyGrowthLoop } = await import('@/services/modules/GrowthLoop')
      destroyGrowthLoop()
    } catch {
      // 动态导入失败，忽略
    }

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
    // 这些状态会在 sessionListStore.fetchSessions 恢复时被使用，
    // 让重登后自动回到退出前的会话。
    // 注：会话被主动删除时由 sessionListStore 单独清理此 key（合理）。
    // localStorage.removeItem(STORAGE_KEYS.LAST_ACTIVE_SESSION) // ← 不再删除

    // 清除状态
    set({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      error: null,
    })
  },

  /** 刷新令牌（单一互斥源） 调用后端 POST /api/v1/auth/refresh 端点刷新访问令牌。 */
  refreshToken: async () => {
    // 已有 in-flight 刷新：复用，不重复打后端
    if (refreshInFlight) {
      return refreshInFlight
    }

    refreshInFlight = (async () => {
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

        // 刷新成功后重新调度主动刷新（新的 expires_in）
        scheduleTokenRefresh()
      } catch (error: unknown) {
        // refreshToken 失败时只抛错不主动 logout，由调用方按错误类型决策。
        if (isAuthFailureFromError(error)) {
          try {
            const { destroyGrowthLoop } = await import('@/services/modules/GrowthLoop')
            destroyGrowthLoop()
          } catch {
            // 动态导入失败，忽略
          }
        }
        // 刷新失败，抛出错误让调用方决策，不主动 logout。
        // 用 cause 保留原始错误，调用方可通过 isAuthFailureFromError 判断错误类型。
        throw new Error('令牌刷新失败，请重新登录', { cause: error })
      }
    })()

    // 无论成功失败都清空 in-flight，允许下次重新尝试
    refreshInFlight.finally(() => {
      refreshInFlight = null
    })

    return refreshInFlight
  },

  /** 初始化认证状态（从localStorage恢复） 如果存储的token有效，恢复认证状态并获取最新用户信息。 */
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
              // 刷新成功后标记已认证，触发 initializeGrowthLoop() 重建工作区标签。
              set({ isAuthenticated: true, isInitializing: false })
              // scheduleTokenRefresh 已在 refreshToken 成功时调度，无需重复
              return
            } catch (refreshError) {
              // 等用户下次操作或网络恢复后再尝试。
              if (isAuthFailureFromError(refreshError)) {
                // refresh_token 真正失效，登出
                await get().logout()
                set({ isInitializing: false })
                return
              } else {
                // 暂时性故障（网络/超时/5xx）：保留旧 token，不登出，
                // 让用户停留在未认证状态，网络恢复后可继续使用旧会话状态。
                // 不设置 isAuthenticated=true（旧 token 已过期），但保留工作区状态。
                set({ isInitializing: false })
                return
              }
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

        // 恢复后安排主动刷新（页面刷新恢复的 token 同样需要续期）
        scheduleTokenRefresh()

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

  /** 检查token是否过期 */
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

  /** 获取当前用户信息 调用后端 GET /api/v1/auth/me 端点获取用户信息。 */
  fetchCurrentUser: async () => {
    const userInfo = await authApi.getCurrentUser()
    const user = mapUserInfoToUser(userInfo)

    // 持久化用户信息（使用 STORAGE_KEYS 常量）
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))

    set({ user })
  },

  /** 清除错误 */
  clearError: () => {
    set({ error: null })
  },
}))

/** 注册认证过期回调 当 services/api/client.ts 检测到认证过期时， */
registerAuthExpiredCallback(async () => {
 // 认证过期时 await destroyGrowthLoop 确保完全清理
  try {
    const { destroyGrowthLoop } = await import('@/services/modules/GrowthLoop')
    destroyGrowthLoop()
  } catch {
    // 动态导入失败，忽略
  }

  useAuthStore.setState({
    user: null,
    token: null,
    refreshTokenValue: null,
    isAuthenticated: false,
    error: null,
  })
})
