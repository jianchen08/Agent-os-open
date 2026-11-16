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
import { classifyLoginFailure, loginFailureBanner } from '../services/api/loginFailure'
import { transitionCacheUser } from '../services/auth/cacheBoundary'
import {
  getAccessToken,
  getRefreshTokenValue,
  setTokens,
  clearTokens,
  isExpired,
  isAuthFailureFromError,
  loadMirroredAuthSession,
  refresh,
  scrubLegacyTokenStorages,
  startAutoRefresh,
  stopAutoRefresh,
  onTokenChanged,
} from '../services/auth/tokenLifecycle'
import { registerAuthExpiredCallback } from '../services/authCallbacks'
import { borrowSession, borrowViaShellDevice, mintLocalKernelToken } from '../services/deviceBorrow'
import { enrollAfterLogin } from '../services/deviceEnroll'
import { queryClient } from '../services/query/queryClient'
import { getServerBaseUrl, refreshTokenStorageKey, resolveApiBase } from '../services/shellApiBase'
import { reconcileShellBackendMode } from '../services/shellBackendMode'
import {
  loadShellLocalCredential,
  markShellAutoLoginDisabled,
  notifyShellLoginOutcome,
  shellAutoLoginDisabled,
} from '../services/shellLocalAuth'
import type { LoginResponse, RefreshResponse, UserInfoResponse } from '../types/api'
import type { User } from '../types/models'

/**
 * 远端借权换发（目标内核签发会话 → 令牌按 @基址 分键落盘）。
 * 返回落盘的 refresh token（失败 null）。两个调用点共用：启动前置与存量令牌
 * 失效后的重借。
 *
 * 双腿按序、先成先得：①本机内核代持（桌面主腿——本机内核常驻，mint 后经
 * 本机 sync_agent 握手）；②壳层配对设备直借（移动主腿——远端档本机栈按设计
 * 不拉起，①必然 mint 失败；壳层 per-device token 与心跳同源，直打云端 relay
 * /borrow_auth；桌面无壳凭据恒 null，行为不变）。两腿共享同一落盘点。
 */
async function borrowRemoteSession(customBase: string): Promise<string | null> {
  const storeRefresh = (refreshToken: string): string | null => {
    try {
      localStorage.setItem(refreshTokenStorageKey(customBase), refreshToken)
      return refreshToken
    } catch {
      // 存储不可用：保持未认证（调用方回落自动登录/手动登录）
      return null
    }
  }
  const localToken = await mintLocalKernelToken()
  if (localToken) {
    const borrowed = await borrowSession(customBase, localToken)
    if (borrowed.ok && borrowed.refreshToken) return storeRefresh(borrowed.refreshToken)
  }
  const shelled = await borrowViaShellDevice(customBase)
  if (shelled.ok && shelled.refreshToken) return storeRefresh(shelled.refreshToken)
  return null
}

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

/** 登录/注册成功后的公共收尾：落令牌 + 启动主动续期 + 拉取用户信息。
 * 返回落定的用户（获取失败返回 null——调用方据此跳过切户清账的身份翻转） */
async function persistSessionAndLoadUser(
  response: LoginResponse | RefreshResponse,
  set: (partial: Partial<AuthState>) => void,
  context: '登录' | '注册',
): Promise<User | null> {
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
    // 登录即入网（ADR 2026-11-02-login-as-enrollment，副链路）：admin 登录成功
    // 即设备自动入网，失败不阻塞登录主流程（服务内部只告警）。
    void enrollAfterLogin(user)
    return user
  } catch (_userError) {
    // 获取用户信息失败不伪造 'unknown' 用户持久化——伪造用户写入 localStorage
    // 会掩盖故障（错误的不一致即掩盖）
    localStorage.removeItem(STORAGE_KEYS.AUTH_USER)
    const userError = _userError instanceof Error ? _userError.message : '获取用户信息失败'
    set({
      user: null,
      error: `${context}成功但获取用户信息失败：${userError}，请重新登录`,
    })
    return null
  }
}

/** 本页面会话入网排程去重（认证恢复路径多入口触达；登录路径经
 * persistSessionAndLoadUser 直调不受此限）。用户面以 store 现值为准。 */
let enrollScheduled = false
function scheduleEnrollFromStore(): void {
  if (enrollScheduled) return
  enrollScheduled = true
  void enrollAfterLogin(useAuthStore.getState().user)
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
      // 本机档自愈计数面复位（S5-A③）：成功登录=自愈达成证据，看门狗闩锁与
      // 连续计数同清。尽力而为面，不阻塞登录主流程。
      void notifyShellLoginOutcome(true)
      const user = await persistSessionAndLoadUser(response, set, '登录')

      // 切户清账（隔离执法点②）：跨户翻转（含令牌过期后换户）统一 purge 全部
      // 持久化缓存面——zustand persist/查询持久缓存/用户维 localStorage 键，
      // 旧账户缓存对新账户不可见（1018 实证缺陷：maintenance 登录闪现 admin
      // 会话）。同户重入（装机版自动登录）不 purge，缓存秒开保持。
      if (user) {
        transitionCacheUser(user.id)
      }

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
        // 记日志即止：自生长闭环是登录副链路，失败不回滚登录主流程
        // （OBS-R258-1 吞错误规则登记）
        console.error('登录后启动自生长闭环失败:', err)
      }
    } catch (error: unknown) {
      // 三类失败区别呈现（S5-A②/S5-D①）：限速锁=终态锁定文案不诱导重试、
      // 凭据错=后端原文、网络失败=无法连接内核+目标基址；横幅文案单点在 store。
      const failure = classifyLoginFailure(error)
      const errorMessage = loginFailureBanner(
        failure,
        resolveApiBase(),
        error instanceof Error ? error.message : undefined,
      )
      set({ isLoading: false, error: errorMessage })
      // 凭据错上报壳层计数面（S5-A③ 漂移自愈）：仅凭据类——429 限速锁不是凭据
      // 证据（锁窗内重播种只会在锁内空转重启），网络失败无凭据语义。kind 随报
      // 过桥（原生类别闸执法依据：仅 credential 计数，boot 期连接失败不进面）。
      if (failure.kind === 'credential') {
        void notifyShellLoginOutcome(false, 'credential')
      }
      // 重抛原错误（附着 code/retry_after_secs 分类字段）：LoginModal 自动链
      // 据此识别 429 终态停链；重抛 new Error 会丢分类面（消费方只见 message）
      throw error
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
      await persistSessionAndLoadUser(response, set, '注册').then((user) => {
        // 切户清账（隔离执法点②，同 login）
        if (user) transitionCacheUser(user.id)
      })

      // 认证态翻转即清空全部查询缓存（同 login：注册即登录）
      queryClient.removeQueries()

      // 注册成功后 await restartGrowthLoop 确保模块就绪
      try {
        const { restartGrowthLoop } = await import('@/services/modules/GrowthLoop')
        await restartGrowthLoop()
      } catch (err) {
        // 记日志即止：自生长闭环是注册副链路，失败不回滚注册主流程
        // （OBS-R258-1 吞错误规则登记）
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
    // 登出意图优先于移动壳本机档免填自动登录（sessionStorage 旗标，应用重启即清）
    markShellAutoLoginDisabled()
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
    // 切户清账（隔离执法点②）：会话结束即回收本账户全部持久化缓存面，
    // 下一个登录者（无论谁）从干净态开始。
    transitionCacheUser(null)
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
      // 壳后端模式归位（先于一切登录链：本机档必须先拉起本机栈，否则免填
      // 自动登录打在没起来的 127.0.0.1 上=「无法连接内核」；残留 remote 的
      // 病灶见 shellBackendMode.reconcileShellBackendMode 头注）
      reconcileShellBackendMode()
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
          .then(() => scheduleEnrollFromStore())
          .catch(() => {
            // 获取失败，静默处理
          })
        return
      }

      let storedRefreshToken = getRefreshTokenValue()
      if (!storedRefreshToken) {
        // localStorage 批量提交被强杀丢失的兜底：从主进程镜像回读种子
        // （自定义后端模式 loadMirroredAuthSession 恒 null——镜像文件是桌面
        // 本机内核专属优化，不跨后端承载令牌）
        const mirrored = await loadMirroredAuthSession()
        if (mirrored) {
          localStorage.setItem(refreshTokenStorageKey(), mirrored)
          storedRefreshToken = mirrored
        }
      }
      // 启动即指向远端 = 配对设备借权免登（ADR 2026-10-22 借权）：本机内核代持
      // per-device token 向目标借权，落地后走常规 refresh 恢复——全程零登录面
      // （否则远端档启动即落登录闸，用户 2026-10-22 实测报障）。
      //
      // 借权在两处触发：①无存量令牌；②存量令牌失效（认证失败分支）——目标内核
      // 重启导致令牌作废时必须重新借权，否则坏 token 短路自动登录（当时只清了
      // 凭据没再借 → 又落登录页，用户 2026-10-22 实测二次撞墙）。
      const customBase = getServerBaseUrl()
      if (customBase && !storedRefreshToken) {
        storedRefreshToken = await borrowRemoteSession(customBase)
      }

      if (storedRefreshToken) {
        // 页面刷新恢复：refresh 轮换换取新 token 对
        try {
          await refresh()
          // fetchCurrentUser 经 me 带回首登改密标记（D1-4）
          await get().fetchCurrentUser()
          set({ isAuthenticated: true, isInitializing: false })
          scheduleEnrollFromStore()
          return
        } catch (refreshError) {
          if (isAuthFailureFromError(refreshError)) {
            // 存量 refresh 已死（目标内核换签名密钥重启/过期/吊销）：清凭据后
            // 继续走下方免填自动登录链，不 return——return 会短路装机/壳自动
            // 登录，存量坏 token 把「开箱即用+无密码切回本机」劣化成每次手输
            // （2026-10-22 用户实测报障根因）。此处是初始化清理非用户登出意图：
            // 不走 logout()（它会落壳自动登录禁用旗标，把下方壳凭据链一并掐死）。
            clearTokens()
            transitionCacheUser(null)
            // 远端档且存量令牌已死 → 重新借权再试一轮（坏 token 不能让用户
            // 落登录页：借权链就是为「令牌失效后仍免登」而设）
            if (customBase) {
              const fresh = await borrowRemoteSession(customBase)
              if (fresh) {
                try {
                  await refresh()
                  await get().fetchCurrentUser()
                  set({ isAuthenticated: true, isInitializing: false })
                  scheduleEnrollFromStore()
                  return
                } catch {
                  // 重新借权后仍失败：落入下方自动登录链
                }
              }
            }
            // 落入下方免填自动登录链；链全空才落手动登录
          } else {
            // 暂时性故障（网络/超时/5xx）：保留凭据，不登出，
            // 让用户停留在未认证状态，网络恢复后可继续恢复。
            set({ isInitializing: false })
            return
          }
        }
      }

      // 没有任何可恢复凭据：装机版自动登录（ADR 2026-10-02-packaged-auto-login-env-only）
      // ——宿主提供凭据（仅 app.isPackaged 且 launcher 已播种
      // AGENTOS_ADMIN_PASSWORD 时非空，env 即口令事实源）时走既有登录链；
      // 未播种=部署 bug，回落手动登录。失败同样回落，不阻断初始化。
      // 闸门：仅本机内核档（未设 server_base_url）。自定义后端（远端内核）
      // 账号体系属对方实例——拿本机口令打远端=凭据外泄面+凭据错触发对方
      // 限速锁（把对方 admin 锁进 429，用户随后的手输也被锁）；远端首登必手输。
      const credential =
        getServerBaseUrl() === null ? await window.electronAPI?.adminCredential?.load?.() : null
      if (credential?.username && credential.password) {
        try {
          await get().login(credential.username, credential.password)
          set({ isInitializing: false })
          return
        } catch {
          // 自动登录失败不阻断初始化，落入下方未认证终态
        }
      }

      // 移动壳本机内核档免填自动登录：凭据=壳设备侧生成口令（KernelHost.
      // localCredentials，SharedPreferences 事实源；非本机档/无壳恒 null）。
      // 用户本次运行内登出过则跳过（登出意图优先）；失败回落手动登录，
      // 内核冷启动窗口由 LoginModal 的本机免填重试循环兜底。
      if (!shellAutoLoginDisabled()) {
        const shellCredential = await loadShellLocalCredential()
        if (shellCredential) {
          try {
            await get().login(shellCredential.username, shellCredential.password)
            set({ isInitializing: false })
            return
          } catch {
            // 内核未就绪等：不阻断初始化，落登录页（免填重试继续兜）
          }
        }
      }

      // 初始化完成（未认证）
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

    // 切户清账（隔离执法点②）：刷新恢复/改密等路径复确认身份，磁盘身份与
    // 缓存命名空间漂移（异常态）在此对齐；同户为 no-op。
    transitionCacheUser(user.id)

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

  // 切户清账（隔离执法点②）：认证失效=会话终止，整页刷新前回收持久化缓存面
  // （重载后自动登录如换户，旧账户缓存不可见）
  transitionCacheUser(null)

  useAuthStore.setState({
    user: null,
    token: null,
    refreshTokenValue: null,
    isAuthenticated: false,
    error: null,
  })
})
