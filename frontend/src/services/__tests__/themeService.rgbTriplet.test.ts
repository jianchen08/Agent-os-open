/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * themeService — RGB 三元组镜像变量（--ds-accent-*-rgb / --ds-status-*-rgb）单测
 *
 * 契约（WebView 110 兼容，R11）：
 * - hexToRgbTriplet：hex（6/3 位）→ "r g b" 三元组，非法输入返回 null
 * - compileThemeVariables：accent/status 桥接变量同源镜像出 -rgb 三元组，
 *   源值非 hex（glow 阴影串）时缺席；三元组值恒为三个 0-255 整数
 */
import { describe, it, expect } from 'vitest'
import { presetThemes } from '@/config/themes'
import { compileThemeVariables, hexToRgbTriplet } from '@/services/themeService'
import type { ThemeConfig } from '@/types/theme'

describe('hexToRgbTriplet — hex → "r g b" 三元组', () => {
  it('6 位 hex 正常换算（dark 主题实际色值）', () => {
    expect(hexToRgbTriplet('#22D3EE')).toBe('34 211 238')
    expect(hexToRgbTriplet('#A78BFA')).toBe('167 139 250')
    expect(hexToRgbTriplet('#60A5FA')).toBe('96 165 250')
  })

  it('3 位 hex 边界展开（#abc → #aabbcc）', () => {
    expect(hexToRgbTriplet('#abc')).toBe('170 187 204')
    expect(hexToRgbTriplet('#F00')).toBe('255 0 0')
  })

  it('非法输入返回 null（空串/非法字符/非 hex 颜色语法）', () => {
    expect(hexToRgbTriplet('')).toBeNull()
    expect(hexToRgbTriplet('#12')).toBeNull()
    expect(hexToRgbTriplet('#GGHHII')).toBeNull()
    expect(hexToRgbTriplet('rgb(16, 185, 129)')).toBeNull()
    // dark 主题 --ds-accent-glow 的实际源值：阴影串非 hex，不可静态取三元组
    expect(hexToRgbTriplet('0 0 15px rgba(34, 211, 238, 0.3)')).toBeNull()
  })

  it('性质：任意合法 hex 输出的三元组恒为三个 0-255 整数（空白分隔）', () => {
    for (const hex of ['#000000', '#FFFFFF', '#123456', '#f0f8ff', '#0af']) {
      const parts = hexToRgbTriplet(hex)!.split(' ')
      expect(parts).toHaveLength(3)
      for (const p of parts) {
        const n = Number(p)
        expect(Number.isInteger(n)).toBe(true)
        expect(n).toBeGreaterThanOrEqual(0)
        expect(n).toBeLessThanOrEqual(255)
      }
    }
  })
})

describe('compileThemeVariables — 三元组镜像注入', () => {
  const DARK = presetThemes['dark'] as ThemeConfig

  it('accent/status 桥接变量同源镜像 -rgb 三元组（与 --ds-* 值一致）', () => {
    const css = compileThemeVariables(DARK)
    expect(css).toContain('--ds-accent-primary-rgb: 34 211 238')
    expect(css).toContain('--ds-accent-ai-rgb: 167 139 250')
    expect(css).toContain('--ds-accent-blue-rgb: 59 130 246')
    expect(css).toContain('--ds-status-info-rgb: 96 165 250')
    expect(css).toContain('--ds-status-error-rgb: 248 113 113')
  })

  it('源值非 hex 时三元组缺席（glow 阴影串不镜像，消费方类内 fallback 兜底）', () => {
    const css = compileThemeVariables(DARK)
    expect(css).toContain('--ds-accent-glow:')
    expect(css).not.toContain('--ds-accent-glow-rgb')
  })

  it('性质：全部 -rgb 镜像值均为三个 0-255 整数（消费契约 rgb(var(--x-rgb)/a)）', () => {
    const matches = compileThemeVariables(DARK).match(/--ds-(?:accent|status)-[a-z]+-rgb: [^;]+/g) ?? []
    expect(matches.length).toBeGreaterThan(0)
    for (const m of matches) {
      const parts = m.split(': ')[1].split(' ')
      expect(parts).toHaveLength(3)
      for (const p of parts) {
        const n = Number(p)
        expect(Number.isInteger(n)).toBe(true)
        expect(n).toBeGreaterThanOrEqual(0)
        expect(n).toBeLessThanOrEqual(255)
      }
    }
  })
})
