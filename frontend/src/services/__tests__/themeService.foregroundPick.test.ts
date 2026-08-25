/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * themeService — 语义前景黑白择优计算测试
 *
 * 真实缺陷：--primary-foreground 曾取 bubble.user_text（对着气泡底调的值），
 * 压在 colors.primary 上在 5/7 主题对比度 2.2~3.5（pixel-art 2.23 接近隐形）；
 * --secondary/accent-foreground 取 text.primary 同样跨槽位撞色。改为按底色
 * 黑白择优后，用性质断言兜底：任何主题、任何语义槽位，算出的前景对其底色
 * 对比度必须 ≥ 4.5（AA 正文）。
 */

import { describe, it, expect } from 'vitest'
import { compileThemeVariables } from '@/services/themeService'
import { presetThemes } from '@/config/themes'

function parseVars(cssVars: string): Record<string, string> {
  const out: Record<string, string> = {}
  cssVars
    .split(';')
    .filter((v) => v.trim())
    .forEach((entry) => {
      const idx = entry.indexOf(':')
      if (idx > 0) {
        out[entry.slice(0, idx).trim()] = entry.slice(idx + 1).trim()
      }
    })
  return out
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const m = hex.replace(/^#/, '').match(/^([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i)
  return m ? { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) } : null
}

function hslRawToRgb(raw: string): { r: number; g: number; b: number } | null {
  // "H S% L%" 原始格式 → 近似 rgb（供对比度复算）
  const m = raw.match(/^(\d+)\s+(\d+)%\s+(\d+)%$/)
  if (!m) return null
  const h = +m[1] / 360
  const s = +m[2] / 100
  const l = +m[3] / 100
  const f = (n: number) => {
    const k = (n + h * 12) % 12
    const a = s * Math.min(l, 1 - l)
    return Math.round(255 * (l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1))))
  }
  return { r: f(0), g: f(8), b: f(4) }
}

function luminance({ r, g, b }: { r: number; g: number; b: number }): number {
  const f = (v: number) => {
    const n = v / 255
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function contrast(a: { r: number; g: number; b: number }, b: { r: number; g: number; b: number }): number {
  const l1 = luminance(a)
  const l2 = luminance(b)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

describe('语义前景黑白择优计算', () => {
  it('性质：全部主题的 primary/secondary/accent 前景对其底色 ≥ 4.5', () => {
    for (const theme of Object.values(presetThemes)) {
      const vars = parseVars(compileThemeVariables(theme))
      for (const slot of ['primary', 'secondary', 'accent']) {
        const bgRaw = vars[`--${slot}`]
        const fgRaw = vars[`--${slot}-foreground`]
        const bg = hslRawToRgb(bgRaw)
        const fg = hslRawToRgb(fgRaw)
        expect(bg, `${theme.id} --${slot} 应为 HSL 原始格式`).toBeTruthy()
        expect(fg, `${theme.id} --${slot}-foreground 应为 HSL 原始格式`).toBeTruthy()
        const ratio = contrast(fg!, bg!)
        expect(
          ratio,
          `${theme.id} ${slot}-foreground 对比度 ${ratio.toFixed(2)} 不达 AA`,
        ).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('性质：全部主题全部状态色的前景（黑白择优）对其状态色 ≥ 4.5', () => {
    for (const theme of Object.values(presetThemes)) {
      const vars = parseVars(compileThemeVariables(theme))
      for (const [key, value] of Object.entries(theme.colors.status)) {
        const fgRaw = vars[`--status-${key}-foreground`]
        const fg = hexToRgb(fgRaw)
        const bg = hexToRgb(value)
        expect(fg, `${theme.id} --status-${key}-foreground 缺失`).toBeTruthy()
        expect(bg, `${theme.id} status.${key} 非 HEX，无法复算`).toBeTruthy()
        const ratio = contrast(fg!, bg!)
        expect(ratio, `${theme.id} status.${key} 前景对比度 ${ratio.toFixed(2)}`).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('字面锚点：dark 主题主色 #22D3EE 偏亮 → 前景取黑（旧值 bubble.user_text 路径已退役）', () => {
    const vars = parseVars(compileThemeVariables(presetThemes['dark']))
    expect(vars['--primary-foreground']).toBe('0 0% 0%')
  })

  it('字面锚点：high-contrast 主色纯白 → 前景取黑', () => {
    const vars = parseVars(compileThemeVariables(presetThemes['high-contrast']))
    expect(vars['--primary-foreground']).toBe('0 0% 0%')
  })

  it('状态 rgb 三元组输出（tailwind alpha 修饰消费），与状态色同值', () => {
    const theme = presetThemes['light']
    const vars = parseVars(compileThemeVariables(theme))
    const rgb = hexToRgb(theme.colors.status.error)
    expect(vars['--status-error-rgb']).toBe(`${rgb!.r} ${rgb!.g} ${rgb!.b}`)
    expect(vars['--status-error-rgb']).toMatch(/^\d+ \d+ \d+$/)
  })

  it('性质：底色为 rgba / 渐变 / 不可解析时，状态前景仍取黑白且亮暗方向正确', () => {
    const base = presetThemes['dark']
    const mk = (info: string) => ({
      ...base,
      colors: { ...base.colors, status: { ...base.colors.status, info } },
    })
    // rgba 亮底 → 黑；渐变取色标中位（深）→ 白；垃圾值 → 回退白
    const varsRgba = parseVars(compileThemeVariables(mk('rgba(240, 240, 240, 0.9)')))
    expect(varsRgba['--status-info-foreground']).toBe('#000000')
    const varsGradient = parseVars(compileThemeVariables(mk('linear-gradient(90deg, #111827 0%, #050a0c 100%)')))
    expect(varsGradient['--status-info-foreground']).toBe('#ffffff')
    const varsJunk = parseVars(compileThemeVariables(mk('not-a-color')))
    expect(varsJunk['--status-info-foreground']).toBe('#ffffff')
  })
})
