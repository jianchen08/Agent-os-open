/**
 * 登录即入网（ADR 2026-11-02-login-as-enrollment）——前端编排面。
 *
 * 唯一认证动作 = 连接 IP + 账号密码登录；admin 登录成功即设备自动入网，
 * 无任何用户侧配对动作（生成入网码/输码 UI 已退役）。两条腿：
 * - 移动壳：`window.LingxiSync` 在场 → 壳 joinWithAuth(jwt, base)（壳以当前
 *   连接基址的 admin JWT 直签自身设备身份）；
 * - 桌面：本机内核 sync_agent `POST /ext/sync_agent/enroll` 代理直签（本机
 *   admin JWT 过本机闸，relay 的 admin JWT 经出站透传）——relay 基址先试当前
 *   连接基址，未成再回退 localStorage 按基址分键的已存会话（`refresh_token@<base>`
 *   换发为 admin JWT）：远程 hub 拓扑下「从本浏览器登录过一次 hub」即满足，
 *   正是登录模型。
 *
 * 副链路口径（仿 restartGrowthLoop/notifyShellLoginOutcome 先例）：任何失败只
 * console.warn，不阻塞登录主流程；未入网的可见面 = 设备注册表无本机 +
 * sync_agent status paired=false。本页面会话入网判定一次（状态查询幂等跳过；
 * 解除配对后下次页面加载自动重入网）。
 */
import axios from 'axios'
import { extUrl, EXT_ROUTE } from '@/services/api/extRoute'
import { ensureFreshToken } from '@/services/auth/tokenLifecycle'
import { mintLocalKernelToken, unwrapBody } from '@/services/deviceBorrow'
import { resolveApiBase } from '@/services/shellApiBase'
import type { User } from '@/types/models'

const RELAY_PLUGIN_ID = 'sync_relay'
const REFRESH_KEY_PREFIX = 'refresh_token@'
const REQUEST_TIMEOUT_MS = 15_000

/** 本机内核专用客户端（baseURL='' = 同源=本机内核，与远端基址无关；deviceBorrow 同律） */
const localClient = axios.create({ timeout: 30_000 })

/** 壳侧同步模块最小桥面（app-mobile/src/sync/index.ts 的 LingxiSyncApi 子集）。 */
interface ShellSyncBridge {
  joinWithAuth?: (jwt: string, apiBase?: string) => Promise<unknown>
  engine?: { status: () => { paired: boolean } }
}

/** joinWithAuth 已验在场的壳桥（shellSync 出口契约：入网腿可直接调用）。 */
type ShellSyncReady = ShellSyncBridge & {
  joinWithAuth: (jwt: string, apiBase?: string) => Promise<unknown>
}

function shellSync(): ShellSyncReady | null {
  const bridge = (globalThis as { LingxiSync?: ShellSyncBridge }).LingxiSync
  // typeof 守卫即缺失态显式分支：无 joinWithAuth 的壳 → null（调用面走桌面腿）
  return bridge && typeof bridge.joinWithAuth === 'function' ? (bridge as ShellSyncReady) : null
}

function errMessage(err: unknown): string {
  const e = err as { response?: { data?: { error?: { message?: string } | string } }; message?: string }
  const inner = e?.response?.data?.error
  if (typeof inner === 'string' && inner) return inner
  if (inner && typeof inner === 'object' && inner.message) return inner.message
  return e?.message ?? String(err)
}

/** 本机 sync_agent 配对状态（同源=本机内核；不可达=按未配对待入网面）。 */
async function localPaired(): Promise<boolean> {
  try {
    const resp = await localClient.get(extUrl('sync_agent', '/status'))
    return unwrapBody(resp.data)?.paired === true
  } catch {
    return false
  }
}

/** 入网尝试结果：ok=已入网；forbidden=本机闸拒绝（换 relay 基址无意义，终止）；failed=可试下一候选。 */
type EnrollAttempt = 'ok' | 'forbidden' | 'failed'

async function tryEnrollViaSyncAgent(base: string, relayJwt: string, localJwt: string): Promise<EnrollAttempt> {
  // 尽力而为前置：relay 基址未启用 sync_relay 则先启用（已启用/失败均不在此
  // 定夺——后续 enroll 出站自会暴露真实可达性，告警留痕即可）。
  try {
    await axios.put(
      `${base}/api/v1/plugins/${RELAY_PLUGIN_ID}/enabled`,
      { enabled: true },
      { headers: { Authorization: `Bearer ${relayJwt}` }, timeout: REQUEST_TIMEOUT_MS },
    )
  } catch (err) {
    console.warn(`[device-enroll] 启用 sync_relay 未成（${base}）:`, errMessage(err))
  }
  try {
    const resp = await localClient.post(
      extUrl('sync_agent', '/enroll'),
      // relay 端点基址（sync_agent 出站寻址事实源）；EXT_ROUTE 拼接防手写漂移
      { relay_url: `${base.replace(/\/+$/, '')}${EXT_ROUTE}/${RELAY_PLUGIN_ID}`, admin_jwt: relayJwt },
      { headers: { Authorization: `Bearer ${localJwt}` } },
    )
    return unwrapBody(resp.data)?.paired === true ? 'ok' : 'failed'
  } catch (err) {
    const status = (err as { response?: { status?: number } })?.response?.status
    if (status === 403) {
      console.warn('[device-enroll] 本机内核拒绝入网调用（非 admin/身份不符），终止')
      return 'forbidden'
    }
    console.warn(`[device-enroll] 经 ${base} 入网未成:`, errMessage(err))
    return 'failed'
  }
}

/** 已存会话换发 admin JWT（refresh_token@<base> → access token；失败回 null）。 */
async function refreshStoredSession(base: string): Promise<string | null> {
  try {
    const stored = localStorage.getItem(REFRESH_KEY_PREFIX + base)
    if (!stored) return null
    const resp = await axios.post(
      `${base}/api/v1/auth/refresh`,
      { refresh_token: stored },
      // Authorization 显式空串=不带认证（refresh_token 只经 body，client.ts 同律）
      { headers: { Authorization: '' }, timeout: REQUEST_TIMEOUT_MS },
    )
    const token = (resp.data as { access_token?: unknown })?.access_token
    return typeof token === 'string' && token ? token : null
  } catch {
    return null
  }
}

/**
 * 桌面腿：本机 sync_agent 直签入网。relay 候选基址 = 当前连接基址（可能是远程
 * hub）优先，再回退本浏览器已存会话的各基址；本机闸凭据 = 当前连接即本机时用
 * 当前 JWT，远端档用 mint（装机/壳本机凭据）。
 */
async function enrollLocalKernel(currentBase: string, currentJwt: string): Promise<void> {
  if (await localPaired()) return
  const localJwt = currentBase ? await mintLocalKernelToken() : currentJwt
  if (!localJwt) {
    console.warn('[device-enroll] 本机内核身份不可得（远端档且本机凭据未播种），跳过入网')
    return
  }
  const bases: string[] = [currentBase || window.location.origin]
  for (let i = 0; i < localStorage.length; i += 1) {
    const key = localStorage.key(i)
    if (!key?.startsWith(REFRESH_KEY_PREFIX)) continue
    const stored = key.slice(REFRESH_KEY_PREFIX.length)
    if (!bases.includes(stored)) bases.push(stored)
  }
  for (const base of bases) {
    const relayJwt = base === (currentBase || window.location.origin) ? currentJwt : await refreshStoredSession(base)
    if (!relayJwt) continue
    const attempt = await tryEnrollViaSyncAgent(base, relayJwt, localJwt)
    if (attempt !== 'failed') return
  }
  console.warn('[device-enroll] 全部候选 relay 均未入网（设备注册表将保持无本机）')
}

/**
 * 登录成功后入网（login/注册/自动登录恢复后的统一入口；幂等可重入）。
 * admin 门控：签发面本就 admin 专属，非 admin 登录不入网（无提权），静默跳过
 * 是设计裁定非错误。user 由调用方传入（authStore 侧消费点，避免环依赖）。
 */
export async function enrollAfterLogin(user: Pick<User, 'role'> | null): Promise<void> {
  try {
    if (!user || user.role !== 'admin') return
    const jwt = await ensureFreshToken()
    if (!jwt) return
    const base = resolveApiBase()
    const shell = shellSync()
    if (shell) {
      if (shell.engine?.status?.().paired) return
      await shell.joinWithAuth(jwt, base || undefined)
      return
    }
    await enrollLocalKernel(base, jwt)
  } catch (err) {
    console.warn('[device-enroll] 登录入网未完成（副链路不阻塞登录）:', errMessage(err))
  }
}
