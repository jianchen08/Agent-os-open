/** 认证状态管理 Store：UI 态（user/isAuthenticated/error）与登录编排。
 *
 * token 生命周期（存取/过期判定/互斥刷新/主动续期调度/认证失效分类）已收口到
 * services/auth/tokenLifecycle 唯一实现（同一职责必须内聚——散落五处且
 * 每处失败都静默）。本 store 不再直接读写
 * localStorage 令牌键，一律经 tokenLifecycle。
 */

import { create } from 'zustand'
import { STORAGE_KEYS } from '../constants/storage'
import { queryClient } from '../services/query/queryClient'
import * as authApi from '../services/api/auth'
import {
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  clearTokens,
  isExpired,
  isAuthFailureFromError,
  refresh,
  scrubLegacyTokenStorages,
  startAutoRefresh,
  stopAutoRefresh,
  onTokenChanged,
} from '../services/auth/tokenLifecycle'
import { registerAuthExpiredCallback } from '../services/authCallbacks'
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
  /** 首登强制改密（D1-4）：播种账号 login 响应携带，改密成功后清除 */
  mustChangePassword: boolean
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
  /** 修改口令（验旧→写新哈希→吊销其他会话；响应携带新 token 对） */
  changePassword: (oldPassword: string, newPassword: string) => Promise<void>
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
  mustChangePassword: false,
  isLoading: false,
  isInitializing: true, // 初始状态为true，表示正在初始化
  error: null,

  /** 登录 调用后端 POST /api/v1/auth/login 端点进行认证。 */
  login: async (username, password) => {
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
        mustChangePassword: response.must_change_password === true,
        isLoading: false,
        error: null,
      })
      await persistSessionAndLoadUser(response, set, '登录')

      // 认证态翻转即清空全部查询缓存：登录前已挂载的 query（sessions 等）
      // 以旧身份拉取的数据对新用户不可见即弃（invalidate 的重拉失败时旧数据
      // 仍滞留展示——同标签页 admin→注册新用户实测泄露旧会话列表，深度测试
      // 2026-09-11 晚）。removeQueries 移除后活动观察者立即以新身份重拉。
      // 登录失败不走到这里，不触发清空。
      queryClient.removeQueries()

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
        mustChangePassword: response.must_change_password === true,
        isLoading: false,
        error: null,
      })
      await persistSessionAndLoadUser(response, set, '注册')

      // 认证态翻转即清空全部查询缓存（同 login：注册即登录）
      queryClient.removeQueries()

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
    localStorage.removeItem(STORAGE_KEYS.AUTH_USER)

    set({
      user: null,
      token: null,
      refreshTokenValue: null,
      isAuthenticated: false,
      mustChangePassword: false,
      error: null,
    })
  },

  /** 修改口令：成功后落新 token 对并清除强制改密标记。 */
  changePassword: async (oldPassword, newPassword) => {
    const response = await authApi.changePassword(oldPassword, newPassword)
    setTokens(
      response.access_token,
      response.refresh_token ?? getRefreshTokenValue() ?? '',
      response.expires_in,
    )
    startAutoRefresh()
    set({
      token: response.access_token,
      refreshTokenValue: response.refresh_token ?? getRefreshTokenValue(),
      mustChangePassword: false,
      error: null,
    })
  },

  /**
   * 初始化认证状态：令牌有效性判定与恢复刷新全部经 tokenLifecycle。
   * access token 仅存内存——页面刷新后恒走 refresh 轮换链路恢复（refresh
   * token 持久存 localStorage，浏览器重启后仍自动登录）；同页 store 重建
   * （内存 token 仍有效）则直接恢复，不白耗一次轮换。
   */
  initializeAuth: async () => {
    try {
      // 升级残留清擦：access token 两键与 sessionStorage 的 refresh 键任何
      // 版本都非法（storage 面现状见 tokenLifecycle 头注）
      scrubLegacyTokenStorages()
      const storedUser = localStorage.getItem(STORAGE_KEYS.AUTH_USER)

      const memoryToken = getAccessToken()
      if (memoryToken && !isExpired()) {
        // 内存 token 仍有效（同页重建）：直接恢复认证状态
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
          token: memoryToken,
          refreshTokenValue: getRefreshTokenValue(),
          isAuthenticated: true,
          isInitializing: false,
        })
        // 恢复后安排主动刷新
        startAutoRefresh()
        // 异步获取最新用户信息
        get()
          .fetchCurrentUser()
          .catch(() => {
            // 获取失败，静默处理
          })
        return
      }

      const storedRefreshToken = getRefreshTokenValue()
      if (storedRefreshToken) {
        // 页面刷新恢复：refresh 轮换换取新 token 对
        try {
          await refresh()
          // fetchCurrentUser 经 me 带回首登改密标记（D1-4）
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
          // 暂时性故障（网络/超时/5xx）：保留凭据，不登出，
          // 让用户停留在未认证状态，网络恢复后可继续恢复。
          set({ isInitializing: false })
          return
        }
      }

      // 没有任何可恢复凭据，初始化完成
      set({ isInitializing: false })
    } catch (_error) {
      // 存储不可用或其他错误，安全降级
      set({ isInitializing: false })
    }
  },

  /** 获取当前用户信息 调用后端 GET /api/v1/auth/me 端点获取。 */
  fetchCurrentUser: async () => {
    const userInfo = await authApi.getCurrentUser()
    const user = mapUserInfoToUser(userInfo)

    // 持久化用户信息
    localStorage.setItem(STORAGE_KEYS.AUTH_USER, JSON.stringify(user))

    // 首登改密标记以 me 为统一出口（登录/刷新/恢复各路径都会走到这里）
    set({ user, mustChangePassword: userInfo.must_change_password === true })
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
