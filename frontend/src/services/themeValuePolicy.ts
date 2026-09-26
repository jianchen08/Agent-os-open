/**
 * 主题值域策略：theme.apply 载荷深校验 + 颜色值解析函数族（值域本身是 CSS）。
 *
 * 独立模块：themeService 为冻结巨型文件只许缩小不许增长（any 棘轮门禁），
 * 值域校验/解析新增面落此处。单向依赖：本模块不 import themeService。
 */
import type { ThemeConfig } from '@/types/theme'

/**
 * 深校验载荷内全部字符串值（validateThemeConfig 结构校验全过后追加执法）。
 *
 * 载荷最终经 compileThemeVariables 变成 CSS 变量值注入宿主子树。语料实相：
 * colors.bubble 携带 radius/shadow 复合串、colors.background.main 可为渐变——
 * **值域本身就是 CSS**，颜色词法不可行；注入面由 url()/绝对 http/超长封禁
 * 覆盖（远程外联探针/追踪像素/javascript: URI 全部封死），相对路径与 data:
 * 保持可用。
 */

/** 主题内任意字符串值的统一上限（防超长载荷注入 CSS 变量值） */
const THEME_MAX_STRING_LENGTH = 512

/** 深遍历载荷内全部字符串执法（数组与嵌套对象均下钻）。 */
export function validateThemeValuesDeep(theme: Partial<ThemeConfig>): string[] {
  const errors: string[] = []
  const visit = (value: unknown, path: string): void => {
    if (typeof value === 'string') {
      const label = `${path} 值非法`
      if (value.length > THEME_MAX_STRING_LENGTH) {
        errors.push(`${label}: 超长（>${THEME_MAX_STRING_LENGTH} 字符）`)
      }
      if (/url\s*\(/i.test(value)) {
        errors.push(`${label}: 禁止 url()（主题载荷不携带外链资源）`)
      }
      if (/^https?:\/\//i.test(value)) {
        errors.push(`${label}: 禁止绝对 http(s) 地址（外链资源走相对路径或 data:）`)
      }
      if (/javascript:/i.test(value)) {
        errors.push(`${label}: 非法协议`)
      }
    } else if (Array.isArray(value)) {
      value.forEach((v, i) => visit(v, `${path}[${i}]`))
    } else if (value && typeof value === 'object') {
      for (const [k, v] of Object.entries(value)) visit(v, `${path}.${k}`)
    }
  }
  if (theme.colors && typeof theme.colors === 'object') visit(theme.colors, 'colors')
  for (const pillar of ['components', 'effects', 'backgrounds'] as const) {
    const value = theme[pillar]
    if (value && typeof value === 'object') visit(value, pillar)
  }
  return errors
}

/**
 * 将 HEX 颜色值转换为 RGB 对象
 *
 * @param hex - HEX 颜色值（如 #3b82f6 或 #fff）
 * @returns RGB 对象，如果解析失败则返回 null
 */
export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const match = hex.replace(/^#/, '').match(/^([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i)
  if (!match) return null
  return {
    r: parseInt(match[1], 16),
    g: parseInt(match[2], 16),
    b: parseInt(match[3], 16),
  }
}

/**
 * 将任意颜色值解析为 RGB（HEX / rgb(a) / 渐变取色标中位近似）
 *
 * @param color - 颜色值字符串
 * @returns RGB 对象（不含 alpha），无法解析时返回 null
 */
export function colorToRgb(color: string): { r: number; g: number; b: number } | null {
  if (!color || typeof color !== 'string') return null
  if (color.startsWith('#')) return hexToRgb(color)

  const rgbaMatch = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgbaMatch) {
    return { r: parseInt(rgbaMatch[1]), g: parseInt(rgbaMatch[2]), b: parseInt(rgbaMatch[3]) }
  }

  const solidFromGradient = extractSolidFromGradient(color)
  return solidFromGradient ? hexToRgb(solidFromGradient) : null
}

/**
 * 颜色相对亮度（WCAG 2.1）：0（纯黑）..1（纯白），对比度复算与黑白择优的共用基元
 */
export function relativeLuminance(rgb: { r: number; g: number; b: number }): number {
  const channels = [rgb.r, rgb.g, rgb.b].map((v) => {
    const n = v / 255
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
}

/**
 * 两实色的 WCAG 对比度（2.2:1..21:1），气泡内链接保底判据用
 */
export function wcagRatio(
  a: { r: number; g: number; b: number },
  b: { r: number; g: number; b: number },
): number {
  const la = relativeLuminance(a)
  const lb = relativeLuminance(b)
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * 为给定底色择优前景色（纯白或纯黑，取对比度更高者）
 *
 * 语义前景（primary/secondary/accent/status 前景）的单点计算：声明值面向
 * 各自背景 authored，跨槽位复用必撞色，这里按底色亮度择黑/白保证恒可读。
 * 亮度分界 L≈0.179（黑白对比度相等点），偏亮取黑、偏暗取白。
 *
 * @param bg - 底色值字符串（HEX/rgba/渐变）
 * @returns '#000000' 或 '#ffffff'，无法解析时回退白色（深色底为主）
 */
export function contrastPick(bg: string): string {
  const rgb = colorToRgb(bg)
  if (!rgb) return '#ffffff'
  return relativeLuminance(rgb) > 0.179 ? '#000000' : '#ffffff'
}

/**
 * 将 RGB 值转换为 HSL 格式字符串
 *
 * 输出格式为 shadcn/ui 期望的原始 HSL 值（不含 hsl() 包裹），
 * 如 "210 40% 98%" 或 "210 40% 98% / 0.5"（带透明度）
 *
 * @param r - 红色通道 (0-255)
 * @param g - 绿色通道 (0-255)
 * @param b - 蓝色通道 (0-255)
 * @param alpha - 可选透明度 (0-1)
 * @returns HSL 格式字符串
 */
function rgbToHsl(r: number, g: number, b: number, alpha?: number): string {
  const rn = r / 255
  const gn = g / 255
  const bn = b / 255
  const max = Math.max(rn, gn, bn)
  const min = Math.min(rn, gn, bn)
  const l = (max + min) / 2
  let h = 0
  let s = 0

  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case rn:
        h = ((gn - bn) / d + (gn < bn ? 6 : 0)) / 6
        break
      case gn:
        h = ((bn - rn) / d + 2) / 6
        break
      case bn:
        h = ((rn - gn) / d + 4) / 6
        break
    }
  }

  const hDeg = Math.round(h * 360)
  const sPct = Math.round(s * 100)
  const lPct = Math.round(l * 100)

  if (alpha !== undefined && alpha < 1) {
    return `${hDeg} ${sPct}% ${lPct}% / ${alpha}`
  }
  return `${hDeg} ${sPct}% ${lPct}%`
}

/**
 * 从渐变等复杂颜色值中提取实色（取色标中位近似整体观感）
 *
 * 渐变字符串塞进 hsl(var(--xxx)) 桥接会全线失效（面板透明），
 * 这里为 shadcn 桥接提取一个可解析的实色近似值。
 *
 * @param color - 颜色值字符串
 * @returns 实色 HEX，无法提取时返回 null
 */
export function extractSolidFromGradient(color: string): string | null {
  const stops = color.match(/#[0-9a-f]{6}\b/gi)
  if (!stops || stops.length === 0) return null
  return stops[Math.floor((stops.length - 1) / 2)]
}

/**
 * 将任意颜色值转换为 HSL 原始格式
 *
 * 支持 HEX (#rrggbb) 和 RGBA (rgba(r,g,b,a)) 格式，
 * 输出 shadcn/ui 期望的 HSL 原始值（用于 hsl(var(--xxx)) 模式）。
 * 渐变值提取色标中位转实色（渐变原样输出会让 hsl() 桥接全线失效）。
 *
 * @param color - 颜色值字符串
 * @returns HSL 格式字符串，解析失败时返回原值
 */
export function colorToHsl(color: string): string {
  if (color.startsWith('#')) {
    const rgb = hexToRgb(color)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  const rgbaMatch = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgbaMatch) {
    const r = parseInt(rgbaMatch[1])
    const g = parseInt(rgbaMatch[2])
    const b = parseInt(rgbaMatch[3])
    const a = rgbaMatch[4] !== undefined ? parseFloat(rgbaMatch[4]) : undefined
    return rgbToHsl(r, g, b, a)
  }

  const solidFromGradient = extractSolidFromGradient(color)
  if (solidFromGradient) {
    const rgb = hexToRgb(solidFromGradient)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  return color
}

/**
 * 将颜色转换为不透明的 HSL 原始格式
 *
 * 与 colorToHsl 相同，但强制忽略 alpha 通道，确保输出为完全不透明；
 * 渐变值同样提取色标中位转实色。
 *
 * @param color - 颜色值字符串
 * @returns 不透明的 HSL 格式字符串
 */
export function colorToHslSolid(color: string): string {
  if (color.startsWith('#')) {
    const rgb = hexToRgb(color)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  const rgbaMatch = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgbaMatch) {
    const r = parseInt(rgbaMatch[1])
    const g = parseInt(rgbaMatch[2])
    const b = parseInt(rgbaMatch[3])
    return rgbToHsl(r, g, b)
  }

  const solidFromGradient = extractSolidFromGradient(color)
  if (solidFromGradient) {
    const rgb = hexToRgb(solidFromGradient)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  return color
}
