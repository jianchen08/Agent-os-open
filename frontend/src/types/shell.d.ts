/**
 * 移动壳运行时面（window 注入）环境类型声明。
 *
 * - LINGXI_BUILD：vite.config.ts 构建期 define 注入（git 短哈希 + 构建时间），
 *   关于页「前端构建标识」数据源。
 * - Capacitor：Capacitor 运行时仅在壳 WebView 存在（native-bridge 注入）；
 *   浏览器/桌面无此全局，消费方必须经 services/shellPlugins.ts 的访问器判空降级，
 *   禁止直接摸 window.Capacitor。
 */

declare const __LINGXI_BUILD__: { git: string; time: string }

interface Window {
  Capacitor?: {
    Plugins?: Record<string, unknown>
  }
}
