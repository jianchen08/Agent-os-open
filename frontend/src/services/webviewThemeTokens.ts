/**
 * Webview 面板主题 token 构造（theme.sync 下行载荷，面板-宿主融合协议）。
 *
 * 独立模块：themeService 为冻结巨型文件只许缩小不许增长（any 棘轮门禁），
 * 面板融合新增面落此处。
 */
import { colorToRgb } from './themeValuePolicy'
import type { ThemeConfig } from '@/types/theme'

/**
 * 构造 Webview 面板主题 token map（theme.sync 下行载荷，面板-宿主融合协议）
 *
 * 固定 12 键（--ag-*）：颜色优先取生效 ThemeConfig 声明值；配置缺省的键回退
 * 宿主 documentElement 当前实际 CSS 变量值（捕获插件皮肤 overlay 等已落地值），
 * 保证面板拿到的都是可用值。--ag-accent-soft 无直接声明字段，由 accent 派生
 * 半透明浅底。只读：不触碰 DOM。
 *
 * @param config - 当前生效主题（会话 override 档优先，否则全局 themeConfig）
 * @param pluginVarOverlay - 插件主题（contributes.themes）声明的 CSS 变量覆盖，
 *        仅覆盖 12 键内命中的项；会话 override 生效时不传（作用域档整体接管）
 */
export function buildWebviewThemeTokens(
  config: ThemeConfig | null | undefined,
  pluginVarOverlay?: Record<string, string> | null,
): Record<string, string> {
  const rootStyle =
    typeof document !== 'undefined'
      ? window.getComputedStyle(document.documentElement)
      : null
  const cssVar = (name: string): string => (rootStyle?.getPropertyValue(name) ?? '').trim()
  const pick = (declared: string | undefined, fallbackVar: string): string =>
    declared || cssVar(fallbackVar)

  const c = config?.colors
  const borderRadius = config?.components?.borderRadius
  const tokens: Record<string, string> = {
    '--ag-bg': pick(c?.background?.main, '--bg-main'),
    '--ag-fg': pick(c?.text?.primary, '--text-primary'),
    '--ag-muted': pick(c?.text?.secondary, '--text-secondary'),
    '--ag-border': pick(c?.border?.default, '--border-default'),
    '--ag-card': pick(c?.background?.card, '--bg-card'),
    '--ag-accent': pick(c?.accent, '--accent'),
    '--ag-accent-soft': accentSoftToken(c?.accent, cssVar),
    '--ag-chip': pick(config?.components?.badge?.variants?.default?.bg, '--badge-default-bg'),
    '--ag-ok': pick(c?.status?.success, '--status-success'),
    '--ag-warn': pick(c?.status?.warning, '--status-warning'),
    '--ag-err': pick(c?.status?.error, '--status-error'),
    '--ag-radius': pick(
      borderRadius?.defaultRadius ? borderRadius[borderRadius.defaultRadius] : undefined,
      '--radius-md',
    ),
  }
  if (pluginVarOverlay) {
    for (const key of Object.keys(tokens)) {
      const overridden = pluginVarOverlay[key]
      if (overridden) tokens[key] = overridden
    }
  }
  return tokens
}

/** 强调浅底：accent 半透明派生；不可解析回退宿主选中底，再退 accent 原值 */
function accentSoftToken(accent: string | undefined, cssVar: (name: string) => string): string {
  if (accent) {
    const rgb = colorToRgb(accent)
    if (rgb) return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, 0.14)`
  }
  return cssVar('--selection-bg') || accent || ''
}
