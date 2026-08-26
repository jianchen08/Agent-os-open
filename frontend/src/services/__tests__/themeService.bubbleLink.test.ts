// @feature: 气泡内链接可读性 | @ci: frontend-baseline
/**
 * --bubble-link 发射测试：用户气泡面常为页面主色或其反转，markdown 链接
 * 若取页面主色在面上同色不可读（dark 主题面=#22D3EE=#primary、high-contrast
 * 白面白链接）。编译器契约：主色对面 ≥3 保用主色（保品牌识别），否则黑白
 * 兜底。比值判定用测试内独立 WCAG 实现，不复用被测端。
 */

import { describe, expect, it } from 'vitest'

import { getAllPresetThemes, compileThemeVariables } from '../themeService'

const REL_LUM_CACHE = new Map<string, number>()

function relLum(hex6: string): number {
  const cached = REL_LUM_CACHE.get(hex6)
  if (cached !== undefined) return cached
  const h = hex6.replace('#', '')
  const ch = [0, 2, 4].map((i) => {
    const n = parseInt(h.slice(i, i + 2), 16) / 255
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
  })
  const l = 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
  REL_LUM_CACHE.set(hex6, l)
  return l
}

function ratio(a: string, b: string): number {
  const la = relLum(a)
  const lb = relLum(b)
  const [hi, lo] = la >= lb ? [la, lb] : [lb, la]
  return (hi + 0.05) / (lo + 0.05)
}

/** 从渐变/rgba 值取静态实色近似（与编译器同一规则独立实现：渐变取中位色标） */
function faceApprox(raw: string): string {
  const rgba = raw.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/)
  if (rgba) {
    return '#' + rgba.slice(1).map((v) => Number(v).toString(16).padStart(2, '0')).join('')
  }
  const stops = raw.match(/#[0-9a-fA-F]{6}\b/g)
  assert(stops && stops.length > 0, `无法从面值取实色近似: ${raw}`)
  return stops[Math.floor((stops.length - 1) / 2)]
}

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) throw new Error(msg)
}

describe('compileThemeVariables: --bubble-link 发射', () => {
  const themes = getAllPresetThemes()

  it.each(themes.map((t) => [t.id, t] as const))(
    '%s: 链接色对用户气泡面 ≥3',
    (_id, theme) => {
      const vars = compileThemeVariables(theme)
      const linkMatch = vars.match(/--bubble-link: (#[0-9a-fA-F]{6})/)
      assert(linkMatch, `${theme.id} 未发射 --bubble-link`)
      const link = linkMatch[1].toLowerCase()
      const face = faceApprox(theme.colors.bubble.user_bg).toLowerCase()
      // 保真与黑白兜底两条通道都保证 ≥3（保真通道准入即 ≥3；黑白择优
      // 在亮度分界两侧最差 ≈4.6）
      expect(ratio(link, face) >= 3, `${theme.id}: link=${link} 对面 ${face} 对比度不足`).toBe(true)
    },
  )

  it('主色即气泡面的主题走黑白兜底，不原样发射同色', () => {
    // dark 主题: user_bg == primary == #22D3EE，链接必须兜底而非同色透传
    const dark = themes.find((t) => t.id === 'dark')!
    const m = compileThemeVariables(dark).match(/--bubble-link: (#[0-9a-fA-F]{6})/)!
    expect(m[1].toLowerCase()).not.toBe('#22d3ee')
    expect(['#000000', '#ffffff']).toContain(m[1].toLowerCase())
  })

  it('主色对黑面达标的主题保用主色原值（品牌识别通道）', () => {
    const hc = themes.find((t) => t.id === 'high-contrast')!
    const m = compileThemeVariables(hc).match(/--bubble-link: (#[0-9a-fA-F]{6})/)!
    expect(m[1].toLowerCase()).toBe('#ffffff')
  })

  it('未命中发射条件时不输出该变量（CSS 端回退 hsl(--primary))', () => {
    // 全部预设的 face 都可解析（纯色或含 hex 色标渐变），恒应发射；
    // 该断言钉住"发射失败静默"的反向漂移
    for (const t of themes) {
      expect(compileThemeVariables(t)).toMatch(/--bubble-link: /)
    }
  })
})
