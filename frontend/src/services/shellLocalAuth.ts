/**
 * 移动壳「本机内核」登录凭据加载（本机免填自动登录的凭据源）。
 *
 * 凭据事实源在设备侧：壳原生（KernelHostPlugin）首启 SecureRandom 生成 admin
 * 口令落应用私有 SharedPreferences（重启同值，不进打包面），经 Capacitor 插件桩
 * 提供给 WebView。仅「本机内核档」（壳注入基址存在且用户未设置非回环自定义
 * 后端）时返回凭据——自定义后端的账号体系属对方实例，凭据必须用户手填（远程
 * 必填）；回环基址（127.0.0.1:9300 形态）豁免，那就是设备自己的内核。
 *
 * 浏览器/桌面无壳桥 → null（桌面自动登录走 electronAPI.adminCredential，
 * ADR 2026-10-02-packaged-auto-login-env-only，与本服务互不干扰）。
 */

import { isLoopbackOrigin } from '@/services/connectionProbe'
import { getServerBaseUrl, getShellApiBase } from '@/services/shellApiBase'
import { getShellKernelHost } from '@/services/shellPlugins'

export interface ShellLocalCredential {
  username: string
  password: string
}

/**
 * 本机免填自动登录的「本次运行内禁用」旗标：用户显式登出后，同一次运行内
 * （含页面刷新）不再自动重登——登出意图优先于开箱即用；旗标存 sessionStorage，
 * 应用重启即清（重启回到零配置自动登录语义）。
 */
const AUTO_LOGIN_DISABLED_KEY = 'lingxi_shell_auto_login_disabled'

export function markShellAutoLoginDisabled(): void {
  try {
    sessionStorage.setItem(AUTO_LOGIN_DISABLED_KEY, '1')
  } catch {
    // 存储不可用：无旗标即不禁用，仅影响登出后的重登体验
  }
}

export function shellAutoLoginDisabled(): boolean {
  try {
    return sessionStorage.getItem(AUTO_LOGIN_DISABLED_KEY) === '1'
  } catch {
    return false
  }
}

export async function loadShellLocalCredential(): Promise<ShellLocalCredential | null> {
  // 自定义后端档：不适用本机凭据（对方实例有自己的账号体系）。
  // 回环基址（存量用户手填 127.0.0.1:9300 形态）豁免——那就是设备自己的内核
  // （2026-10-23 用户令：填回环被判自定义后端=停本机栈+免填失效的死路）。
  const customBase = getServerBaseUrl()
  if (customBase !== null && !isLoopbackOrigin(customBase)) return null
  // 无壳注入基址：无「本机内核」档语义（浏览器/桌面）
  const shellBase = getShellApiBase()
  if (!shellBase) return null
  const plugin = getShellKernelHost()
  if (!plugin?.localCredentials) return null
  try {
    const result = await plugin.localCredentials()
    const username = result?.username
    const password = result?.password
    if (typeof username === 'string' && username && typeof password === 'string' && password) {
      return { username, password }
    }
    return null
  } catch {
    // 桥不可用（旧包 WebView 桩缺失等）：回落手动登录，不阻塞
    return null
  }
}

/**
 * 登录结果上报壳层（S5-A③ 漂移自愈计数面）：本机内核档凭据失败连续 ≥3 由壳
 * 触发一次重播种（内核重启按 env 重置口令，漂移与限速锁一并清除），成功登录
 * 复位计数。仅本机内核档上报——自定义后端/浏览器/桌面无此语义；旧包壳无
 * reportLoginOutcome 桩时静默跳过（出桥契约只增不删，双版本共存容错）。
 * kind=失败类别（原生类别闸执法：仅 credential 计数，连接拒绝/超时/5xx 不
 * 计——判类真源在 HTTP 状态唯一可见面 axios 层 classifyLoginFailure，此处
 * 只透传）。旧包前端不带 kind，原生按可用性不计（缺证据不计=fail-safe）。
 * 上报失败不重试不告警：自愈是尽力而为面，登录主流程不受影响。
 */
export async function notifyShellLoginOutcome(
  success: boolean,
  kind?: 'credential' | 'availability',
): Promise<void> {
  const customBase = getServerBaseUrl()
  if (customBase !== null && !isLoopbackOrigin(customBase)) return
  if (!getShellApiBase()) return
  const plugin = getShellKernelHost()
  if (!plugin?.reportLoginOutcome) return
  const payload: { success: boolean; kind?: 'credential' | 'availability' } = { success }
  if (kind) payload.kind = kind
  try {
    await plugin.reportLoginOutcome(payload)
  } catch {
    // 桥调用失败：自愈计数面失效可接受（退化为既有「重启应用自愈」路径）
  }
}
