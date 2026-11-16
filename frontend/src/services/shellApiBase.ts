/**
 * 移动壳（Capacitor 本地直用模式）API 基址单点。
 *
 * 壳本地模式：前端 dist 打进 APK，页面 origin 是壳本地地址（http://localhost），
 * 同源相对路径不再命中云端内核。基址由壳侧注入的 /shell-config.js 提供——
 * syncweb（app-mobile/scripts/syncweb.mjs）从 capacitor.config.json 的
 * plugins.ShellConfig.apiBase 生成该文件，并在 www/index.html head 注入
 * <script src>；head 经典脚本先于 deferred 模块脚本执行，时序有规范保证。
 *
 * 解析优先级（连接设置页消费同一单点，壳轮询脚本按同键联动）：
 *   用户设置 localStorage[server_base_url] > ShellConfig.apiBase > 同源（空串）。
 *
 * 浏览器/桌面（云端同源或 Vite 代理）：无用户设置、壳不注入 → 返回空串，行为与
 * 无本文件时完全一致。消费方 = constants/api.ts 的 API_BASE_URL
 * （apiClient baseURL 与 WS 派生均由此单点跟随）+ refreshTokenStorageKey
 * （登录凭据按基址分键，services/auth/tokenLifecycle 消费）。
 *
 * 注：Capacitor 8 无 window.Capacitor.getConfig()（JS 运行时与 native-bridge
 * 均未定义，配置读取只在原生侧 Plugin.getConfigJSON）——壳内注入读取是
 * Capacitor 8 下的等价机制（选型记录见 E轨迹汇聚落地_台账 2026-10-13 本地直用刀）。
 */

import { STORAGE_KEYS } from '@/constants/storage'

/** 用户侧服务器地址（连接设置页写入；壳轮询脚本读同键联动） */
export const SERVER_BASE_URL_KEY = 'server_base_url'

/** 壳注入的静态配置（apiBase + 版本面，由 syncweb 生成） */
export interface LingxiShellConfig {
  apiBase?: string
  versionName?: string
  versionCode?: number
}

function readShellConfig(): LingxiShellConfig | null {
  const cfg = (window as unknown as { LINGXI_SHELL_CONFIG?: unknown }).LINGXI_SHELL_CONFIG
  return cfg && typeof cfg === 'object' ? (cfg as LingxiShellConfig) : null
}

/** 壳注入基址：无壳（浏览器/桌面）返回空串 */
export function getShellApiBase(): string {
  const cfg = readShellConfig()
  return typeof cfg?.apiBase === 'string' && cfg.apiBase ? cfg.apiBase : ''
}

/** 用户设置的连接地址：未设置/存储异常返回 null（回落下一优先级） */
export function getServerBaseUrl(): string | null {
  try {
    const v = localStorage.getItem(SERVER_BASE_URL_KEY)
    return typeof v === 'string' && v.trim() ? v.trim() : null
  } catch {
    return null
  }
}

/**
 * API 基址解析唯一入口：用户设置 > 壳注入 > 同源（空串）。
 * 移动壳轮询脚本（shell-push.js）对 localStorage 同键做同优先级解析，两处联动。
 */
export function resolveApiBase(): string {
  return getServerBaseUrl() ?? getShellApiBase()
}

/**
 * refresh token 的按后端分键（每后端登录态各自保存，切换往返不互串）：
 * 默认档（基址=壳注入/同源，即「本机内核」档）沿用裸键 refresh_token——
 * 桌面/壳本机模式与既有落盘数据行为不变；自定义后端按基址隔离
 * `refresh_token@<base>`。壳轮询脚本（shell-push.js）以同规则分键联动。
 * 基址在页面会话内稳定（切换后端以整页刷新收尾，见连接设置页）。
 */
export function refreshTokenStorageKey(apiBase: string = resolveApiBase()): string {
  return apiBase && apiBase !== getShellApiBase()
    ? `${STORAGE_KEYS.REFRESH_TOKEN}@${apiBase}`
    : STORAGE_KEYS.REFRESH_TOKEN
}

/** 壳注入的版本面（versionName/versionCode，syncweb 自 build.gradle 生成）；无壳返回 null */
export function getShellAppVersion(): { versionName?: string; versionCode?: number } | null {
  const cfg = readShellConfig()
  if (!cfg || (cfg.versionName === undefined && cfg.versionCode === undefined)) return null
  return { versionName: cfg.versionName, versionCode: cfg.versionCode }
}
