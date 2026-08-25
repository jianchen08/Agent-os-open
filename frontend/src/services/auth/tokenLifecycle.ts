/**
 * Token 生命周期单一职责模块（同一职责必须内聚）。
 *
 * token 生命周期散落五处——authStore 主动续期定时器 / HTTP 401 拦截 /
 * WS 4001 重连刷新 / initializeAuth 恢复 / visibilitychange 检查——外加 token
 * 存取三入口（tokenManager / authStorage / 直接 localStorage）。每处失败都是
 * 静默的，每次故障都在离故障最近处打补丁，是「token 过期修 N 次还在复发」的
 * 架构根因。本模块是 token 存取/过期判定/互斥刷新/主动续期调度/认证失效分类
 * 的唯一实现；authStore 只保留 UI 状态编排，WS/HTTP/visibility 全部经此模块。
 *
 * 依赖方向（防静态环）：authStore/client/GlobalWebSocket/useRealtimeEvents →
 * 本模块（静态）；本模块 → services/api/auth（动态 import，因为 auth.ts →
 * client.ts 会与本模块静态互指，与 client.ts 引 authStore 的避环手法同因）。
 */

import { STORAGE_KEYS } from '@/constants/storage'

/** 判断错误是否为「真正认证失效」（应触发 logout） */
export function isAuthFailureFromError(error: unknown): boolean {
  if (!error) return false
  // 无刷新凭据（refresh 的确定性失败）：重试永远无意义，按认证失败处理，
  // 让 WS 重连流程走 triggerAuthExpired 跳登录——否则被当瞬时故障无限重试，
  // 表现为「未连接」常驻且永不弹登录。
  if ((error as { authNoCredentials?: boolean })?.authNoCredentials === true) return true
  // 直接的 axios 错误
  const directStatus = (error as { response?: { status?: number } })?.response?.status
  if (directStatus === 401 || directStatus === 403) return true
  // 被 refresh 包装的错误（Error with cause）
  const cause = (error as { cause?: unknown })?.cause
  const causeStatus = (cause as { response?: { status?: number } })?.response?.status
  if (causeStatus === 401 || causeStatus === 403) return true
  return false
}

// ──────────────────────────────────────────────
// 存取（唯一入口；收编 tokenManager/authStorage/散落的直接 localStorage 读写）
// ──────────────────────────────────────────────

export function getAccessToken(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN)
  } catch {
    return null
  }
}

export function getRefreshTokenValue(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEYS.REFRESH_TOKEN)
  } catch {
    return null
  }
}

/** 写入令牌三件套（access/refresh/过期时刻）。expires_in 单位为秒。 */
export function setTokens(accessToken: string, refreshToken: string, expiresIn: number): void {
  const expiryTime = Date.now() + expiresIn * 1000
  localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, accessToken)
  localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, refreshToken)
  localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY, expiryTime.toString())
  notifyTokenChanged()
}

export function clearTokens(): void {
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN)
  localStorage.removeItem(STORAGE_KEYS.REFRESH_TOKEN)
  localStorage.removeItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)
  notifyTokenChanged()
}

// ──────────────────────────────────────────────
// 过期判定（唯一实现）
// ──────────────────────────────────────────────

export function isExpired(): boolean {
  try {
    const storedExpiry = localStorage.getItem(STORAGE_KEYS.ACCESS_TOKEN_EXPIRY)
    if (!storedExpiry) return true // 没有过期时间记录，视为已过期
    const expiryTime = parseInt(storedExpiry, 10)
    if (isNaN(expiryTime)) return true
    return Date.now() > expiryTime
  } catch {
    return true
  }
}

// ──────────────────────────────────────────────
// 刷新（互斥单飞；所有触发路径共用一个 in-flight）
// ──────────────────────────────────────────────

let refreshInFlight: Promise<void> | null = null

export async function refresh(): Promise<void> {
  // 已有 in-flight 刷新：复用，不重复打后端
  if (refreshInFlight) {
    return refreshInFlight
  }

  refreshInFlight = (async () => {
    const currentRefreshToken = getRefreshTokenValue()

    if (!currentRefreshToken) {
      // 确定性认证失败（无凭据）：带标记抛出，isAuthFailureFromError 据此走
      // 登出/跳登录，而非按瞬时故障无限重试。
      const err = new Error('没有可刷新的令牌') as Error & { authNoCredentials?: boolean }
      err.authNoCredentials = true
      throw err
    }

    try {
      // 动态 import 防静态环（见文件头注释）
      const authApi = await import('@/services/api/auth')
      const response = await authApi.refreshToken(currentRefreshToken)

      localStorage.setItem(STORAGE_KEYS.ACCESS_TOKEN, response.access_token)
      localStorage.setItem(
        STORAGE_KEYS.ACCESS_TOKEN_EXPIRY,
        String(Date.now() + response.expires_in * 1000),
      )
      // 如果返回了新的 refresh_token，也更新它（后端现为无状态不轮换，防御性保留）
      if (response.refresh_token) {
        localStorage.setItem(STORAGE_KEYS.REFRESH_TOKEN, response.refresh_token)
      }
      notifyTokenChanged()

      // 刷新成功后重新调度主动续期（新的 expires_in）
      startAutoRefresh()
    } catch (error: unknown) {
      // 只抛错不主动 logout：由调用方按 isAuthFailureFromError 分类决策。
      // 用 cause 保留原始错误，调用方可据此判断错误类型。
      throw new Error('令牌刷新失败，请重新登录', { cause: error })
    }
  })()

  // 无论成功失败都清空 in-flight，允许下次重新尝试。
  // catch 兜住清理链自身的 rejection：主 promise 已 return 给调用方处理，
  // 这条 finally 派生链无人消费，失败时会成为 unhandled rejection
  // （浏览器控制台报错 / vitest 计入 Unhandled Errors）。
  refreshInFlight
    .finally(() => {
      refreshInFlight = null
    })
    .catch(() => {})

  return refreshInFlight
}

/**
 * 「用前保证新鲜」：未过期直接返回当前 token；已过期先刷新再返回。
 * 刷新失败（任何类型）返回 null——调用方（WS 连接/回前台重连）不得用过期
 * token 硬连（会 4001 → 重连风暴），交由既有重连机制退避处理。
 */
export async function ensureFreshToken(): Promise<string | null> {
  if (!isExpired()) {
    return getAccessToken()
  }
  try {
    await refresh()
    return getAccessToken()
  } catch {
    return null
  }
}

// ──────────────────────────────────────────────
// 主动续期调度（唯一持有；失败退避不断链）
// ──────────────────────────────────────────────

/** 提前刷新的最大余量（毫秒），避免 TTL 很大时刷新过于提前 */
const TOKEN_REFRESH_MAX_LEAD_MS = 5 * 60 * 1000

/**
 * 续期失败重试参数。旧逻辑刷新失败一次即清 timer 永久放弃，只剩反应式路径
 * （401/WS 4001）兜底——而 WS 已建立时连接不会因 token 过期立即断，token 到期
 * 后没有任何触发点，直到下一次断线重连才 4001。
 */
const REFRESH_RETRY_BASE_MS = 30_000
const REFRESH_RETRY_MAX_MS = 5 * 60 * 1000
const REFRESH_RETRY_MAX_COUNT = 10

let tokenRefreshTimer: ReturnType<typeof setTimeout> | null = null
/** 续期失败后的连续重试计数（成功即清零） */
let refreshRetryCount = 0

/**
 * 安排下一次主动刷新：在过期前 min(剩余寿命 / 2, 5分钟) 时刷新。
 * 每次 login/register/refresh 成功后调用。
 */
export function startAutoRefresh(): void {
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
  const delay = Math.max(1000, Math.min(remainingMs / 2, TOKEN_REFRESH_MAX_LEAD_MS))
  refreshRetryCount = 0
  scheduleRefreshAttempt(delay)
}

/**
 * 调度一次续期尝试。失败时瞬时故障按退避重试（30s→5min 封顶，最多 10 次），
 * 绝不静默断链；确定性认证失败不重试，清 timer 交由反应式路径触发登出。
 * 重试入口不走 startAutoRefresh（它对已过期 token 直接 return，会把退避
 * 重试链掐死在过期时刻）。
 */
function scheduleRefreshAttempt(delay: number): void {
  tokenRefreshTimer = setTimeout(() => {
    refresh()
      .then(() => {
        refreshRetryCount = 0
        startAutoRefresh() // 刷新成功（新的 expires_in）后重新调度
      })
      .catch((err: unknown) => {
        if (isAuthFailureFromError(err)) {
          tokenRefreshTimer = null
          return
        }
        refreshRetryCount += 1
        if (refreshRetryCount > REFRESH_RETRY_MAX_COUNT) {
          console.error(
            '[tokenLifecycle] token 主动续期连续失败 %d 次，停止重试（反应式路径仍可兜底）',
            refreshRetryCount,
          )
          tokenRefreshTimer = null
          return
        }
        const retryDelay = Math.min(
          REFRESH_RETRY_BASE_MS * Math.pow(2, refreshRetryCount - 1),
          REFRESH_RETRY_MAX_MS,
        )
        console.warn(
          '[tokenLifecycle] token 主动续期瞬时失败（第 %d/%d 次），%dms 后重试',
          refreshRetryCount,
          REFRESH_RETRY_MAX_COUNT,
          retryDelay,
        )
        scheduleRefreshAttempt(retryDelay)
      })
  }, delay)
}

/** 停止主动续期（登出时调用） */
export function stopAutoRefresh(): void {
  if (tokenRefreshTimer) {
    clearTimeout(tokenRefreshTimer)
    tokenRefreshTimer = null
  }
}

// ──────────────────────────────────────────────
// token 变化通知（authStore 据此同步 UI 态；消费方无需各自轮询 localStorage）
// ──────────────────────────────────────────────

type TokenChangedListener = (accessToken: string | null, refreshToken: string | null) => void

const tokenChangedListeners = new Set<TokenChangedListener>()

export function onTokenChanged(listener: TokenChangedListener): () => void {
  tokenChangedListeners.add(listener)
  return () => tokenChangedListeners.delete(listener)
}

function notifyTokenChanged(): void {
  const access = getAccessToken()
  const rt = getRefreshTokenValue()
  for (const listener of tokenChangedListeners) {
    try {
      listener(access, rt)
    } catch {
      // 单个监听者异常不影响其他监听者
    }
  }
}
