/** 认证状态管理 Store：UI 态（user/isAuthenticated/error）与登录编排。
 *
 * token 生命周期（存取/过期判定/互斥刷新/主动续期调度/认证失效分类）已收口到
 * services/auth/tokenLifecycle 唯一实现（同一职责必须内聚——散落五处且
 * 每处失败都静默）。本 store 不再直接读写
 * localStorage 令牌键，一律经 tokenLifecycle。
 */

import { create } from 'zustand'
import { STORAGE_KEYS } from '../constants/storage'
import * as authApi from '../services/api/auth'
import { registerAuthExpiredCallback } from '../services/authCallbacks'
import {
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  clearTokens,
  isExpired,
  isAuthFailureFromError,
  refresh,
  startAutoRefresh,
  stopAutoRefresh,
  onTokenChanged,
} from '../services/auth/tokenLifecycle'
import type { LoginResponse, RefreshResponse, UserInfoResponse } from '../types/api'
import type { User } from '../types/models'

/** 认证状态接口 */
interface AuthState {
  /** 当前用户 */
  user: User | null
  /** 访问令牌 */
  token: string | null
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
  /** 初始化认证状态（从localStorage恢复） */
  initializeAuth: () => Promise<void>
  /** 获取当前用户信息 */
  fetchCurrentUser: () => Promise<void>
  /** 清除错误 */
  clearError: () => void
}

// token 变化（tokenLifecycle 刷新/写入/清除）同步进 UI 态——
// GlobalWebSocket/router 等读 useAuthStore.getState().token 的消费面无需各自轮询。
onTokenChanged((accessToken, refreshTokenValue) => {
  useAuthStore.setState({ token: accessToken, refreshTokenValue })
})

/** 将后端用户信息响应映射为前端User模型 */
function mapUserInfoToUser(userInfo: UserInfoResponse): User {
  return {
    id: userInfo.id,
    username: userInfo.username,
    email: userInfo.email,
    role: userInfo.role,
    createdAt: userInfo.created_at,
  }
}

/** 登录/注册成功后的公共收尾：落令牌 + 启动主动续期 + 拉取用户信息 */
async function persistSessionAndLoadUser(
  response: LoginResponse | RefreshResponse,
  set: (partial: Partial<AuthState>) => void,
  context: '登录' | '注册',
): Promise<void> {
  // refresh_token 类型上可选（后端不轮换时省略）：保留现有值兜底
  setTokens(
    response.access_token,
    response.refresh_token ?? getRefreshTokenValue() ?? '',
    response.expires_in,
  )
  startAutoRefresh()

  try {
    const userInfo = await authApi.getCurrentUser()
    const user = mapUserInfoToUser(userInfo)
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))
    set({ user })
  } catch (_userError) {
    // 获取用户信息失败不伪造 'unknown' 用户持久化——伪造用户写入 localStorage
    // 会掩盖故障（错误的不一致即掩盖）
    localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
    const userError = _userError instanceof Error ? _userError.message : '获取用户信息失败'
    set({
      user: null,
      error: `${context}成功但获取用户信息失败：${userError}，请重新登录`,
    })
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
  login: async (username, password) => {
    // 验证输入
    if (!username || !password) {
      throw new Error('用户名和密码不能为空')
    }

    set({ isLoading: true, error: null })

    try {
      const response: LoginResponse = await authApi.login(username, password)

      set({
        token: response.access_token,
        refreshTokenValue: response.refresh_token,
        isAuthenticated: true,
        isLoading: false,
        error: null,
      })
      await persistSessionAndLoadUser(response, set, '登录')

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
  register: async (username, password, email) => {
    // 验证输入
    if (!username || !password) {
      throw new Error('用户名和密码不能为空')
    }
    if (!email) {
      throw new Error('邮箱不能为空')
    }

    set({ isLoading: true, error: null })

    try {
      // 后端注册成功后自动返回token，实现注册即登录
      const response = await authApi.register(username, password, email)

      set({
        token: response.access_token,
        refreshTokenValue: response.refresh_token,
        isAuthenticated: true,
        isLoading: false,
        error: null,
      })
      await persistSessionAndLoadUser(response, set, '注册')

      // 注册成功后 await restartGrowthLoop 确保模块就绪
      try {
        const { restartGrowthLoop } = await import('@/services/modules/GrowthLoop')
        await restartGrowthLoop()
      } catch (err) {
        console.error('注册后启动自生长闭环失败:', err)
      }
    } catch (error: unknown) {
      const errorMessage = error instanceof Error ? error.message : '注册失败'
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /** 登出 调用后端 POST /api/v1/auth/logout 端点并清除本地令牌。 */
  logout: async () => {
    // 登出时 await destroyGrowthLoop 确保完全清理；停止主动续期并清令牌（tokenLifecycle）
    stopAutoRefresh()
    try {
      const { destroyGrowthLoop } = await import('@/services/modules/GrowthLoop')
      destroyGrowthLoop()
    } catch {
      // 动态导入失败，忽略
    }

    try {
      await authApi.logout(getRefreshTokenValue() || '')
    } catch (_error) {
      // 登出API调用失败，仍然清除本地状态
    }

    clearTokens()
    // 这些状态会在 sessionListStore.fetchSessions 恢复时被使用，
    // 让重登后自动回到退出前的会话。
    // 注：会话被主动删除时由 sessionListStore 单独清理此 key（合理）。
    // localStorage.removeItem(STORAGE_KEYS.LAST_ACTIVE_SESSION) // ← 不再删除
    localStorage.removeItem(STORAGE_KEYS.AUTH_USER)

    set({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      error: null,
    })
  },

  /** 初始化认证状态（从localStorage恢复）：令牌有效性判定与恢复刷新全部经 tokenLifecycle。 */
  initializeAuth: async () => {
    try {
      const storedToken = getAccessToken()
      const storedRefreshToken = getRefreshTokenValue()
      const storedUser = localStorage.getItem(STORAGE_KEYS.AUTH_USER)

      if (storedToken) {
        if (isExpired()) {
          // Token 已过期，尝试刷新
          if (storedRefreshToken) {
            try {
              await refresh()
              // 刷新成功，获取用户信息；成功后标记已认证，触发
              // initializeGrowthLoop() 重建工作区标签。
              await get().fetchCurrentUser()
              set({ isAuthenticated: true, isInitializing: false })
              return
            } catch (refreshError) {
              if (isAuthFailureFromError(refreshError)) {
                // refresh_token 真正失效，登出
                await get().logout()
                set({ isInitializing: false })
                return
              }
              // 暂时性故障（网络/超时/5xx）：保留旧 token，不登出，
              // 让用户停留在未认证状态，网络恢复后可继续使用旧会话状态。
              // 不设置 isAuthenticated=true（旧 token 已过期），但保留工作区状态。
              set({ isInitializing: false })
              return
            }
          }
          // 没有 refresh_token，清除所有数据
          await get().logout()
          set({ isInitializing: false })
          return
        }

        // Token 未过期，恢复认证状态
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
        startAutoRefresh()

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

  /** 获取当前用户信息 调用后端 GET /api/v1/auth/me 端点获取。 */
  fetchCurrentUser: async () => {
    const userInfo = await authApi.getCurrentUser()
    const user = mapUserInfoToUser(userInfo)

    // 持久化用户信息
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
