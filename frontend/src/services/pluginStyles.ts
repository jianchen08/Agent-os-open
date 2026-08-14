/**
 * 插件 CSS 注入（contributes.client_styles）
 *
 * 主题插件改"变量"，但"金色蕾丝边框"这类装饰（border-image / 伪元素 / SVG）
 * 通常不是一个变量能表达的，需要一段 CSS 规则 —— 本服务是主题插件的开发者级补充
 * （任务文档「第 1 层补充」）。
 *
 * 安全模型：
 * - CSS 资源经 /ext/{pluginId}{path} 拉取（带 Bearer，仅 Enabled 插件可挂路由）；
 * - 消毒（sanitizeCss）：命中 expression() / javascript: / 外部 @import / behavior:
 *   等危险构造 → 整段拒绝（fail-closed），不注入；
 * - 注入的 <style> 带会话级 nonce（当前无全局 CSP 时 inert；Electron 打包约定
 *   style-src 'self' 'nonce-{dynamic}' 时唯一放行通道，见 task_electron_packaging.md）；
 * - scope='scoped' 时自动加 [data-plugin="{pluginId}"] 前缀，防插件 CSS 全局污染；
 * - 插件 CSS 应引用 var(--ds-*) 主题变量，切换主题时自动跟随。
 *
 * 生命周期：schema 重载时 syncPluginStyles 以注册表为权威 —— 移除已消失的
 * （插件禁用/卸载即从 registry 移除，样式随之清理），注入新增的。
 */

import { apiClient } from '@/services/api/client'
import { loggers } from '@/utils/logger'
import type { ClientStyleDeclaration } from '@/services/schema/ContributionRegistry'

/** <style> 唯一标识属性（值形如 "{pluginId}:{styleId}"，同步时比对用） */
const STYLE_ID_ATTR = 'data-plugin-style'
/** 来源插件属性（移除插件全部样式时按此选择） */
const PLUGIN_ATTR = 'data-plugin'

/** 会话级 nonce：每次会话生成一次，注入的 <style> 均携带 */
let sessionNonce: string | null = null

/**
 * 获取会话级样式 nonce
 *
 * 当前应用文档无全局 CSP（style-src 无限制），nonce 属性 inert；
 * 接入 Electron 打包后 CSP 按 `style-src 'self' 'nonce-{动态nonce}'` 配置时，
 * 带本 nonce 的插件 <style> 是唯一放行通道。
 *
 * @returns 会话级 nonce（无 crypto 环境返回 null，注入时省略 nonce 属性）
 */
export function getStyleNonce(): string | null {
  if (sessionNonce === null && typeof window !== 'undefined' && window.crypto?.randomUUID) {
    sessionNonce = window.crypto.randomUUID()
  }
  return sessionNonce
}

/** 注入防重入集合（同一 styleId 的并发 fetch 只发一次） */
const inflight = new Set<string>()

/**
 * 消毒 CSS：命中危险构造整段拒绝（fail-closed）
 *
 * 过滤目标（任务文档）：
 * - expression() —— IE 动态属性，可执行 JS
 * - javascript: / vbscript: URL（url() 与 @import 均可携带）
 * - @import 外部 URL —— 跨域拉取/绕过管控
 * - behavior: / -moz-binding: —— 旧式绑定，可执行外部行为
 *
 * @param css - 插件 CSS 原文
 * @returns 通过消毒的 CSS；命中危险构造返回 null（调用方跳过注入并 warn）
 */
export function sanitizeCss(css: string): string | null {
  const dangerous: Array<[RegExp, string]> = [
    [/expression\s*\(/i, 'expression()'],
    [/javascript\s*:/i, 'javascript:'],
    [/vbscript\s*:/i, 'vbscript:'],
    [/@import\s+(?:url\s*\()?\s*["']?https?:/i, '外部 @import'],
    [/@import\s+["']?\/\//i, '协议相对 @import'],
    [/behavior\s*:/i, 'behavior:'],
    [/-moz-binding\s*:/i, '-moz-binding:'],
  ]
  for (const [re, label] of dangerous) {
    if (re.test(css)) {
      loggers.websocket.warn(`[pluginStyles] 命中危险 CSS 构造 "${label}"，整段拒绝注入`)
      return null
    }
  }
  return css
}

/**
 * 按 scope 包装 CSS
 *
 * - 'global'（缺省）：原样返回（装饰性全局样式，如金色蕾丝边框）；
 * - 'scoped'：给**顶层**规则选择器加 [data-plugin="{pluginId}"] 前缀。
 *   实现按大括号深度分割顶层规则，@media/@keyframes 等 at-rule 原样保留
 *   （其内部规则不深入加前缀——scoped 模式覆盖常规 UI 规则足够，深层规则
 *   请用插件自身选择器收敛，属文档约定）。
 *
 * @param css - 已消毒 CSS
 * @param pluginId - 来源插件（前缀锚点）
 * @param scope - 声明的作用域
 * @returns 包装后的 CSS
 */
export function scopeCss(css: string, pluginId: string, scope?: 'global' | 'scoped'): string {
  if (scope !== 'scoped') return css
  const prefix = `[data-plugin="${pluginId}"]`

  const parts: string[] = []
  let depth = 0
  let selector = ''
  let body = ''
  for (const ch of css) {
    if (depth === 0) {
      if (ch === '{') {
        depth = 1
        body = ''
      } else {
        selector += ch
      }
    } else {
      if (ch === '{') depth += 1
      else if (ch === '}') {
        depth -= 1
        if (depth === 0) {
          // 顶层规则块闭合：selector.trim() 以 @ 开头为 at-rule（原样保留），
          // 否则对每个逗号分隔的选择器加前缀（{ 前补空格保持可读性）
          const sel = selector.trim()
          if (sel && !sel.startsWith('@')) {
            const scopedSelectors = sel
              .split(',')
              .map((s) => s.trim())
              .filter(Boolean)
              .map((s) => `${prefix} ${s}`)
              .join(', ')
            parts.push(`${scopedSelectors} {${body}}`)
          } else {
            parts.push(`${sel} {${body}}`)
          }
          selector = ''
          body = ''
        }
      }
      if (depth > 0) body += ch
    }
  }
  return parts.join('\n')
}

/**
 * 注入单个插件样式（fetch CSS → 消毒 → scope 包装 → <style> append 到 head）
 *
 * @param style - 插件 CSS 注入声明
 * @returns 是否注入成功（fetch 失败/消毒拒绝/重复注入返回 false）
 */
export async function injectPluginStyle(style: ClientStyleDeclaration): Promise<boolean> {
  const key = `${style.pluginId}:${style.id}`
  // 已注入或注入中：跳过（幂等）
  if (document.querySelector(`style[${STYLE_ID_ATTR}="${key}"]`) || inflight.has(key)) return false
  inflight.add(key)
  try {
    const path = style.path
    const url = `/ext/${style.pluginId}${path.startsWith('/') ? '' : '/'}${path}`
    const res = await apiClient.get<string>(url, {
      responseType: 'text',
      transformResponse: [(d) => d],
    })
    const css = typeof res.data === 'string' ? res.data : String(res.data)
    const clean = sanitizeCss(css)
    if (clean === null) return false

    const el = document.createElement('style')
    el.setAttribute(STYLE_ID_ATTR, key)
    el.setAttribute(PLUGIN_ATTR, style.pluginId)
    const nonce = getStyleNonce()
    if (nonce) el.setAttribute('nonce', nonce)
    el.textContent = scopeCss(clean, style.pluginId, style.scope)
    document.head.appendChild(el)
    loggers.websocket.debug(`[pluginStyles] 注入 ${key} (${css.length} bytes, scope=${style.scope ?? 'global'})`)
    return true
  } catch (e) {
    loggers.websocket.warn(`[pluginStyles] ${key} 加载失败: ${(e as Error)?.message ?? e}`)
    return false
  } finally {
    inflight.delete(key)
  }
}

/**
 * 移除指定插件的全部注入样式
 *
 * @param pluginId - 插件 id
 */
export function removePluginStyles(pluginId: string): void {
  document.querySelectorAll(`style[${PLUGIN_ATTR}="${pluginId}"]`).forEach((el) => el.remove())
}

/**
 * 移除全部插件注入样式（登出/销毁闭环时清理，防止跨会话残留）
 */
export function removeAllPluginStyles(): void {
  document.querySelectorAll(`style[${STYLE_ID_ATTR}]`).forEach((el) => el.remove())
}

/**
 * 以注册表为权威同步注入样式（schema 重载后调用）
 *
 * 1. 移除注册表中已不存在的 <style>（插件禁用/卸载 → 无残留）；
 * 2. 注入注册表中尚未注入的（含重新启用的插件）。
 *
 * @param styles - 当前注册表内全部 client_styles 声明
 */
export function syncPluginStyles(styles: ClientStyleDeclaration[]): void {
  const desired = new Set(styles.map((s) => `${s.pluginId}:${s.id}`))
  document.querySelectorAll(`style[${STYLE_ID_ATTR}]`).forEach((el) => {
    const key = el.getAttribute(STYLE_ID_ATTR)
    if (key && !desired.has(key)) {
      el.remove()
      loggers.websocket.debug(`[pluginStyles] 移除失效样式 ${key}`)
    }
  })
  for (const style of styles) {
    void injectPluginStyle(style)
  }
}
