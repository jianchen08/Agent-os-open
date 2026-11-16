/**
 * 账户命名空间单点（用户隔离执法点①，前端）。
 *
 * 契约：docs/working/用户隔离机制_审计与修复_20261018.md §5——全部持久化缓存
 * （zustand persist / query persister / localStorage 用户维键）经 userScopedKey
 * 注入账户前缀，不同账户读写互不可达；新增持久化面一律走本封装，禁直写共享键名。
 *
 * 身份源取序：内存注册表（authStore 经 setCacheUserId 维护）> localStorage
 * AUTH_USER 回退（页面刷新恢复路径：store 水合先于认证初始化，此时注册表尚空
 * 而磁盘身份已可知）。AUTH_USER 由 authStore 登录后同步写入、登出即删，作回退
 * 事实源与注册表始终同向。
 *
 * 豁免口径（审计报告 §3）：主题/插件健康等设备级偏好同样走本封装（多账户各用
 * 各键，防互写），但参与豁免不参与切户 purge（cacheBoundary 注册表只收账户数据面）。
 */

import { STORAGE_KEYS } from '../constants/storage'

/** 当前账户（内存注册表；null=匿名/未登录） */
let currentUserId: string | null = null
/** 注册表是否已被认证链路显式写入（区分「显式匿名」与「尚未初始化」） */
let registryInitialized = false

/** 从 localStorage AUTH_USER 读身份（刷新恢复路径的回退事实源） */
function identityFromStorage(): string | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEYS.AUTH_USER)
    if (!raw) return null
    const parsed = JSON.parse(raw) as { id?: unknown }
    return typeof parsed.id === 'string' && parsed.id ? parsed.id : null
  } catch {
    // 畸形档（非 JSON/被外力改写）按无身份处理：匿名命名空间不读任何账户数据
    return null
  }
}

/** 当前缓存命名空间所属账户；null=匿名 */
export function currentCacheUserId(): string | null {
  if (registryInitialized) return currentUserId
  return identityFromStorage()
}

/** authStore 在认证态翻转点维护（transitionCacheUser 统一入口）。
 * 显式写入后注册表即权威（含显式 null=登出态，磁盘残影不再回读）。 */
export function setCacheUserId(id: string | null): void {
  currentUserId = id
  registryInitialized = true
}

/**
 * 基础键 → 账户命名空间键。
 *
 * 纯前缀注入不改 base：匿名态 `ag-anon:<base>`（登录页瞬态，purge 覆盖）；
 * 登录态 `ag-u:<userId>:<base>`。升级语义：改名后的键对既有裸键数据天然不可达
 * （旧缓存成为孤儿，由 cacheBoundary 的 legacy 清扫一次性回收）。
 */
export function userScopedKey(base: string): string {
  const id = currentCacheUserId()
  return id ? `ag-u:${id}:${base}` : `ag-anon:${base}`
}
