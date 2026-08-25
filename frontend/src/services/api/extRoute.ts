/**
 * 插件 ext 路由前缀与 URL 拼接
 *
 * 内核 HTTP 命名空间约定：插件 http_endpoints 声明挂载于 `/ext` 前缀下。
 * 具体端点字面量以 endpoints.generated.ts（manifest 投影）为唯一真值源；
 * 本模块只服务声明驱动的通用拼接（任意插件的 webview/CSS/皮肤资产路由，
 * 真值源在插件 manifest，前端不枚举）。
 */

export const EXT_ROUTE = '/ext'

/** 插件 ext 端点 URL（path 缺首斜杠时自动补齐） */
export function extUrl(pluginId: string, path: string): string {
  return `${EXT_ROUTE}/${pluginId}${path.startsWith('/') ? '' : '/'}${path}`
}
