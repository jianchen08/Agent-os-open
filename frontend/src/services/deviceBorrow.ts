/**
 * 配对设备借权连接（ADR 草案 2026-10-22-borrowed-device-auth，用户裁定借权
 * 会话等同 admin）：点设备卡=信任已建立——**本机内核**代持 per-device token
 * 与目标内核完成 borrow_auth 握手，令牌按 @base 分键落盘后整页切换，全程零
 * 手输。
 *
 * 方向铁律（2026-10-22 用户实测修正）：借权请求**永远打本机**（baseURL=''
 * 同源=本机内核），绝不打「当前连接的内核」——后者在已切远端时会拿远端的
 * 身份去借权（身份错位）。本机令牌来源=装机 admin 凭据 / 壳本机凭据。
 *
 * 借权不可用（未配对/本机内核不在/目标拒绝/协议违约）回传 canRetryManual，
 * 卡片呈现「仍要切换」逃生口（落目标登录页，老路径兜底）。
 */
import axios from 'axios'
import { connectKernel } from '@/pages/settings/devices/connectKernel'
import { extUrl } from '@/services/api/extRoute'
import { refreshTokenStorageKey } from '@/services/shellApiBase'
import { loadShellLocalCredential } from '@/services/shellLocalAuth'

export type BorrowOutcome =
  | { ok: true }
  | { ok: false; error: string; canRetryManual: boolean }

export interface BorrowSessionResult {
  ok: boolean
  error?: string
  refreshToken?: string
  accessToken?: string
  /** 失败是否值得用户手动重试（异源不出手/握手失败等可重试态） */
  canRetryManual?: boolean
}

/** 本机内核专用客户端（baseURL='' = 同源=本机内核，与远端基址无关） */
const localClient = axios.create({ timeout: 30000 })

/** axios 错误 → 用户可读消息（内核/插件错误信封两种形态；deviceEnroll 同律） */
function errorMessage(err: unknown, fallback: string): string {
  const e = err as {
    response?: { data?: { error?: { message?: string } | string }; status?: number }
    message?: string
  }
  const inner = e?.response?.data?.error
  if (typeof inner === 'string' && inner) return inner
  if (inner && typeof inner === 'object' && inner.message) return inner.message
  if (e?.response?.status === 403) return '目标内核拒绝借权（本机令牌未通过对方注册表验证）'
  if (e?.response?.status === 503) return '目标内核借权不可用（未配对或管理口令缺失）'
  return e?.message || fallback
}

/** 插件响应体解包（http.handle 契约：data.body=base64(JSON)；兼容直接体形态） */
export function unwrapBody(payload: unknown): Record<string, unknown> | null {
  const outer = payload as { data?: { body?: string }; body?: string }
  const encoded = outer?.data?.body ?? outer?.body
  if (typeof encoded === 'string') {
    try {
      return JSON.parse(atob(encoded)) as Record<string, unknown>
    } catch {
      return null
    }
  }
  return (payload as Record<string, unknown>) ?? null
}

/**
 * 本机内核 admin 令牌：装机凭据（desktop）/ 壳本机凭据（移动壳）→ 本机 login。
 * 借权面必须持本机身份——本机内核不可达时返回 null（调用方显式失败）。
 */
export async function mintLocalKernelToken(): Promise<string | null> {
  let credential: { username?: string; password?: string } | null = null
  const host = (window as unknown as {
    electronAPI?: { adminCredential?: { load?: () => Promise<unknown> } }
  }).electronAPI
  if (host?.adminCredential?.load) {
    const loaded = (await host.adminCredential.load()) as
      | { username?: string; password?: string }
      | null
    credential = loaded ?? null
  } else {
    // shellLocalAuth 已由 LoginModal/authStore 静态入主 chunk（动态拆包不成立），
    // 此处置静态导入（bundle-size 的 INEFFECTIVE_DYNAMIC_IMPORT 棘轮）。
    credential = await loadShellLocalCredential()
  }
  if (!credential?.username || !credential.password) return null
  try {
    const resp = await localClient.post<unknown, { data: { access_token?: string } }>(
      '/api/v1/auth/login',
      { username: credential.username, password: credential.password },
    )
    return resp.data?.access_token ?? null
  } catch {
    return null
  }
}

/**
 * 借权换发（不切换）：本机内核代持令牌向目标内核握手，返回目标内核签发的
 * 令牌对。targetIsHub 缺省=自动判别（先中枢端点后成员端点）。
 */
export async function borrowSession(
  targetBase: string,
  localToken: string,
  targetIsHub?: boolean,
): Promise<BorrowSessionResult> {
  const attempts: boolean[] = targetIsHub === undefined ? [true, false] : [targetIsHub]
  let lastError = '借权握手失败'
  for (const isHub of attempts) {
    try {
      const resp = await localClient.post<unknown, { data: unknown }>(
        extUrl('sync_agent', '/borrow'),
        { target_base: targetBase, target_is_hub: isHub },
        { headers: { Authorization: `Bearer ${localToken}` } },
      )
      const body = unwrapBody(resp.data)
      const refreshToken = typeof body?.refresh_token === 'string' ? body.refresh_token : null
      const accessToken = typeof body?.access_token === 'string' ? body.access_token : null
      if (!refreshToken || !accessToken) {
        lastError = '借权响应缺令牌（目标内核协议违约）'
        continue
      }
      return { ok: true, refreshToken, accessToken }
    } catch (err) {
      lastError = errorMessage(err, '借权握手失败')
      const status = (err as { response?: { status?: number } })?.response?.status
      if (!status) return { ok: false, error: lastError }
      // 端点不存在=判别下一路（中枢/成员）；其余错误直接出
      if (status !== 404 && status !== 502) return { ok: false, error: lastError }
    }
  }
  return { ok: false, error: lastError }
}

/**
 * 壳层配对设备凭据（移动免登腿的事实源）：shell-sync 模块与前端同 WebView
 * 运行时，凭据面共享——Capacitor Preferences 优先、localStorage 同键兜底
 * （与 app-mobile/src/sync/credential.ts CredentialStore 同通道序）。键名与
 * 线格式是跨包契约（CREDENTIAL_KEY），改键名/字段须双侧同刀。
 */
interface ShellSyncDeviceCredential {
  relayUrl: string
  deviceToken: string
}

const SHELL_SYNC_CREDENTIAL_KEY = 'lingxi_sync_credential'

export async function loadShellSyncDeviceCredential(): Promise<ShellSyncDeviceCredential | null> {
  let raw: string | null = null
  const prefs = (
    globalThis as {
      Capacitor?: {
        Plugins?: {
          Preferences?: { get?: (opts: { key: string }) => Promise<{ value?: string | null }> }
        }
      }
    }
  ).Capacitor?.Plugins?.Preferences
  if (prefs && typeof prefs.get === 'function') {
    try {
      raw = (await prefs.get({ key: SHELL_SYNC_CREDENTIAL_KEY })).value ?? null
    } catch {
      raw = null
    }
  }
  if (raw === null) {
    try {
      raw = localStorage.getItem(SHELL_SYNC_CREDENTIAL_KEY)
    } catch {
      return null
    }
  }
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Record<string, unknown>
    const relayUrl = value.relay_url
    const deviceToken = value.device_token
    if (typeof relayUrl === 'string' && relayUrl && typeof deviceToken === 'string' && deviceToken) {
      return { relayUrl, deviceToken }
    }
    return null
  } catch {
    return null
  }
}

/**
 * 移动壳配对设备直借（免登链移动支路，ADR 2026-10-22 借权的移动补全）：
 * 远端档本机栈按设计不拉起（壳模式自愈），「本机内核代持」mint 必然失败——
 * 改用壳层 per-device token（与心跳同源，配对信任已在云端注册表）直打 relay
 * /borrow_auth 换发目标内核会话。不落盘（落盘点统一在 authStore 借权换发）；
 * 403=令牌不识别（未配对/已解绑）、503=对方管理口令缺失，均显式失败回传，
 * 调用方回落手动登录。
 *
 * 已知边界：换发面是**配对中枢**的会话——目标与 relay 异源（不同部署面）时
 * 显式不出手（换来的中枢令牌落错 @键，还会在异源 refresh 面外泄中枢凭据）；
 * 成员目标免登仍走桌面腿（本机 sync_agent 代理）或一次性手输。
 */
export async function borrowViaShellDevice(targetBase: string): Promise<BorrowSessionResult> {
  const credential = await loadShellSyncDeviceCredential()
  if (!credential) return { ok: false, error: '壳层无配对设备凭据（未入网）' }
  if (new URL(credential.relayUrl).hostname !== new URL(targetBase).hostname) {
    return { ok: false, error: '目标与配对中枢异源，设备直借不适用', canRetryManual: true }
  }
  try {
    const resp = await localClient.post(
      `${credential.relayUrl.replace(/\/+$/, '')}/borrow_auth`,
      null,
      { headers: { Authorization: `Bearer ${credential.deviceToken}` } },
    )
    const body = unwrapBody(resp.data)
    const refreshToken = typeof body?.refresh_token === 'string' ? body.refresh_token : null
    const accessToken = typeof body?.access_token === 'string' ? body.access_token : null
    if (!refreshToken || !accessToken) {
      return { ok: false, error: '借权响应缺令牌（目标内核协议违约）' }
    }
    return { ok: true, refreshToken, accessToken }
  } catch (err) {
    return { ok: false, error: errorMessage(err, '借权握手失败'), canRetryManual: true }
  }
}

/**
 * 借权并切换（设备卡入口）：借权 → 令牌按目标基址分键落盘 → connectKernel
 * 探活切换。落盘在切换前完成，刷新后 initializeAuth 以 @base 令牌自动恢复，
 * 全程零登录面。
 */
export async function borrowAndConnect(
  origin: string,
  targetIsHub?: boolean,
): Promise<BorrowOutcome> {
  const localToken = await mintLocalKernelToken()
  if (!localToken) {
    return {
      ok: false,
      error: '本机内核不可达或未播种凭据，借权不可用',
      canRetryManual: true,
    }
  }
  const session = await borrowSession(origin, localToken, targetIsHub)
  if (!session.ok || !session.refreshToken) {
    return { ok: false, error: session.error ?? '借权失败', canRetryManual: true }
  }
  try {
    localStorage.setItem(refreshTokenStorageKey(origin), session.refreshToken)
  } catch {
    return { ok: false, error: '本地存储不可用，未切换连接', canRetryManual: true }
  }
  const switched = await connectKernel(origin)
  if (!switched.ok) {
    return { ok: false, error: switched.error, canRetryManual: true }
  }
  return { ok: true }
}
