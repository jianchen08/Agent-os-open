/**
 * 认证过期回调注册机制
 *
 * 用于解耦 services 层与 stores 层的循环依赖。
 * services/api/client.ts 通过 triggerAuthExpired 触发回调，
 * stores/authStore.ts 在初始化时通过 registerAuthExpiredCallback 注册具体实现。
 */

/** 认证过期回调函数类型 */
type AuthExpiredCallback = () => void

/** 当前注册的认证过期回调 */
let authExpiredCallback: AuthExpiredCallback | null = null

/**
 * 注册认证过期回调
 * @param callback - 认证过期时执行的回调函数
 */
export function registerAuthExpiredCallback(callback: AuthExpiredCallback): void {
  authExpiredCallback = callback
}

/**
 * 触发认证过期回调
 * 由 services/api/client.ts 在认证过期时调用，
 * 实际执行的是 stores/authStore.ts 注册的清除逻辑。
 */
export function triggerAuthExpired(): void {
  if (authExpiredCallback) {
    authExpiredCallback()
  }
}
