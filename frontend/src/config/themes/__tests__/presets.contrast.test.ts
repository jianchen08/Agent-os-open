/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * 预设主题对比度门禁（WCAG 2.1，阈值 = 主题技能文档 §5.1 的硬性门禁表）
 *
 * 背景：预设主题的静态配色从未被机械校验，validateThemeConfig 只查结构缺失，
 * 导致大量"文字色≈背景色"对混入仓库（moe-soft text.disabled 1.37、
 * pixel-art status.success 1.56 等，用户报告"很多主题文字和背景一样"）。
 * 本门禁把对比度变成可回归的机械闸：改主题配色必须过阈，否则 CI 红。
 *
 * 阈值：正文/次要/状态/气泡/组件变体文字 ≥ 4.5（AA 正文）；
 * muted/disabled（辅助可见即可）≥ 3.0。
 * 渐变背景取全部色标逐一校验（最差色标必须达标）；半透明表面
 * （card/input/elevated 为 rgba 时）压在 main 实色近似上合成。
 */

import { describe, it, expect } from 'vitest'
import { presetThemes } from '@/config/themes'
import type { ThemeConfig } from '@/types/theme'

interface Rgb {
  r: number
  g: number
  b: number
  a: number
}

function parseColor(str: string | undefined): Rgb | null {
  if (!str || typeof str !== 'string') return null
  const hex = str.match(/^#([0-9a-f]{6})$/i)
  if (hex) {
    return { r: parseInt(hex[1].slice(0, 2), 16), g: parseInt(hex[1].slice(2, 4), 16), b: parseInt(hex[1].slice(4, 6), 16), a: 1 }
  }
  const rgba = str.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgba) return { r: +rgba[1], g: +rgba[2], b: +rgba[3], a: rgba[4] !== undefined ? +rgba[4] : 1 }
  return null
}

function gradientStops(str: string | undefined): Rgb[] {
  if (!str || typeof str !== 'string' || !str.includes('gradient(')) return []
  return (str.match(/#[0-9a-f]{6}\b/gi) || []).map((s) => parseColor(s)!).filter(Boolean)
}

function backgroundsOf(value: string | undefined): Rgb[] {
  const solid = parseColor(value)
  return solid ? [solid] : gradientStops(value)
}

function composite(fg: Rgb, bg: Rgb): Rgb {
  return {
    r: Math.round(fg.r * fg.a + bg.r * (1 - fg.a)),
    g: Math.round(fg.g * fg.a + bg.g * (1 - fg.a)),
    b: Math.round(fg.b * fg.a + bg.b * (1 - fg.a)),
    a: 1,
  }
}

function luminance({ r, g, b }: Rgb): number {
  const f = (v: number) => {
    const n = v / 255
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function contrast(fg: Rgb, bg: Rgb): number {
  const l1 = luminance(fg)
  const l2 = luminance(bg)
  return (Math.max(l1, l2) + 0.05) / (Math.min(l1, l2) + 0.05)
}

/** 半透明前景/背景压在底色上合成后取对比度；半透明底压在主题基调实色上近似 */
function contrastOn(fgValue: string | undefined, bgValue: string | undefined, baseSolid?: Rgb): number[] {
  const fgs = backgroundsOf(fgValue)
  let bgs = backgroundsOf(bgValue)
  if (fgs.length === 0 || bgs.length === 0) return []
  // 半透明底（badge/toast 的状态色 tint、半透明表面）压在主题基调实色上
  if (baseSolid) {
    bgs = bgs.map((bg) => (bg.a < 1 ? composite(bg, baseSolid) : bg))
  }
  return fgs.flatMap((fg) => bgs.map((bg) => contrast(fg.a < 1 ? composite(fg, bg) : fg, bg)))
}

interface SurfaceSet {
  main: string
  card: string
  input: string
  elevated: string
}

/** 表面集：半透明表面压在 main 实色上近似（card/input/elevated 常为 rgba） */
function surfacesOf(theme: ThemeConfig): Array<[string, string]> {
  const mainStops = gradientStops(theme.colors.background.main)
  const mainSolid = mainStops[0] ?? parseColor('#000000')!
  const out: Array<[string, string]> = [['main', theme.colors.background.main]]
  for (const slot of ['card', 'input', 'elevated'] as const) {
    const value = theme.colors.background[slot]
    const solid = parseColor(value)
    if (solid && solid.a < 1 && mainStops.length > 0) {
      out.push([slot, `rgb(${composite(solid, mainStops[0]).r}, ${composite(solid, mainStops[0]).g}, ${composite(solid, mainStops[0]).b})`])
    } else {
      out.push([slot, value])
    }
  }
  return out
}

const AA = 4.5
const VISIBLE = 3.0

describe('预设主题对比度门禁', () => {
  describe('文字色 on 背景位', () => {
    for (const theme of Object.values(presetThemes)) {
      it(`${theme.id}: primary/secondary ≥4.5, muted/disabled ≥3.0`, () => {
        const failures: string[] = []
        for (const [slot, bgValue] of surfacesOf(theme)) {
          for (const [key, threshold] of [
            ['primary', AA],
            ['secondary', AA],
            ['muted', VISIBLE],
            ['disabled', VISIBLE],
          ] as const) {
            for (const ratio of contrastOn(theme.colors.text[key], bgValue)) {
              if (ratio < threshold) {
                failures.push(`text.${key} on bg.${slot}: ${ratio.toFixed(2)} < ${threshold}`)
              }
            }
          }
        }
        expect(failures, failures.join('\n')).toEqual([])
      })
    }
  })

  describe('状态色 on 主背景', () => {
    for (const theme of Object.values(presetThemes)) {
      it(`${theme.id}: 全部 status ≥4.5`, () => {
        const failures: string[] = []
        for (const [key, value] of Object.entries(theme.colors.status)) {
          for (const ratio of contrastOn(value, theme.colors.background.main)) {
            if (ratio < AA) failures.push(`status.${key}: ${ratio.toFixed(2)} < ${AA}`)
          }
        }
        expect(failures, failures.join('\n')).toEqual([])
      })
    }
  })

  describe('消息气泡', () => {
    for (const theme of Object.values(presetThemes)) {
      it(`${theme.id}: user/ai 气泡文字 ≥4.5`, () => {
        const baseSolid = gradientStops(theme.colors.background.main)[0] ?? parseColor(theme.colors.background.main) ?? parseColor('#000000')!
        const failures: string[] = []
        for (const side of ['user', 'ai'] as const) {
          for (const ratio of contrastOn(theme.colors.bubble[`${side}_text`], theme.colors.bubble[`${side}_bg`], baseSolid)) {
            if (ratio < AA) failures.push(`bubble.${side}: ${ratio.toFixed(2)} < ${AA}`)
          }
        }
        expect(failures, failures.join('\n')).toEqual([])
      })
    }
  })

  describe('组件变体（按钮/徽章/Toast/标签页）', () => {
    for (const theme of Object.values(presetThemes)) {
      it(`${theme.id}: 变体文字 on 变体底 ≥4.5`, () => {
        // 半透明 tint 底压在主题基调实色上（badge/toast 的状态色 tint 实际渲染
        // 在卡片/面板上，当独立实色算会把深色主题全部误判为撞色）
        const baseSolid = gradientStops(theme.colors.background.main)[0] ?? parseColor(theme.colors.background.main) ?? parseColor('#000000')!
        const failures: string[] = []
        const btn = theme.components.button?.variants ?? {}
        for (const [name, v] of Object.entries(btn)) {
          for (const ratio of contrastOn(v.text, v.bg, baseSolid)) {
            if (ratio < AA) failures.push(`btn.${name}: ${ratio.toFixed(2)} < ${AA}`)
          }
        }
        for (const comp of ['badge', 'toast'] as const) {
          const variants = theme.components[comp]?.variants ?? {}
          for (const [name, v] of Object.entries(variants)) {
            for (const ratio of contrastOn(v.text, v.bg, baseSolid)) {
              if (ratio < AA) failures.push(`${comp}.${name}: ${ratio.toFixed(2)} < ${AA}`)
            }
          }
        }
        const tabs = theme.components.tabs
        if (tabs) {
          for (const ratio of contrastOn(tabs.activeText, tabs.activeBg, baseSolid)) {
            if (ratio < AA) failures.push(`tabs.active: ${ratio.toFixed(2)} < ${AA}`)
          }
          for (const ratio of contrastOn(tabs.inactiveText, tabs.listBg, baseSolid)) {
            if (ratio < AA) failures.push(`tabs.inactive: ${ratio.toFixed(2)} < ${AA}`)
          }
        }
        expect(failures, failures.join('\n')).toEqual([])
      })
    }
  })
})
