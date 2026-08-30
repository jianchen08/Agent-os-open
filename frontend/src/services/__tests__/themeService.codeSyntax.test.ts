/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * themeService — 代码块语法色令牌分发测试
 *
 * 契约：--code-bg 与全套 --code-* 语法字色由主题层同源分发——底色亮发浅色
 * 调色板（全部令牌对底色 AA ≥ 4.5），底色暗发深色调色板（oneDark 原生值，
 * 深色主题观感沿用）。分发按代码块底色实际亮度判深浅，与主题 category 解耦：
 * high-contrast 属 special 族但 input 是深底，必须拿深色调色板。
 *
 * 真实缺陷背景：语法字色曾是组件内 oneDark 静态调色板（为深底 hsl(220,13%,18%)
 * 调的），浅色主题 --code-bg 取 input 近白底后，字符串绿约 2.0:1、数字橙约
 * 2.5:1，且主题层防撞色强制（contrastPick）够不到组件内联样式。
 */

import { describe, it, expect } from 'vitest'
import { presetThemes } from '@/config/themes'
import { compileThemeVariables } from '@/services/themeService'

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

/** 解析主题分发的色值（#hex / rgb(a)，rgba 忽略 alpha 按实色读） */
function parseColorToRgb(color: string): { r: number; g: number; b: number } | null {
  const hex = color.trim().match(/^#([0-9a-f]{6})$/i)
  if (hex) {
    const n = parseInt(hex[1], 16)
    return { r: (n >> 16) & 0xff, g: (n >> 8) & 0xff, b: n & 0xff }
  }
  const rgba = color.trim().match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/)
  if (rgba) return { r: +rgba[1], g: +rgba[2], b: +rgba[3] }
  return null
}

function luminance({ r, g, b }: { r: number; g: number; b: number }): number {
  const f = (v: number) => {
    const n = v / 255
    return n <= 0.03928 ? n / 12.92 : ((n + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function contrast(
  a: { r: number; g: number; b: number },
  b: { r: number; g: number; b: number },
): number {
  const la = luminance(a)
  const lb = luminance(b)
  return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05)
}

const SYNTAX_VARS = [
  '--code-text',
  '--code-comment',
  '--code-keyword',
  '--code-string',
  '--code-number',
  '--code-function',
  '--code-tag',
  '--code-attr',
] as const

/** oneDark 原生调色板（深底配套；深色族沿用的观感锚点） */
const ONE_DARK: Record<string, string> = {
  '--code-text': '#abb2bf',
  '--code-comment': '#5c6370',
  '--code-keyword': '#c678dd',
  '--code-string': '#98c379',
  '--code-number': '#d19a66',
  '--code-function': '#61afef',
  '--code-tag': '#e06c75',
  '--code-attr': '#d19a66',
}

describe('代码块语法色令牌分发', () => {
  it('性质：全部主题都发出 --code-bg 与全套 --code-* 语法令牌', () => {
    for (const theme of Object.values(presetThemes)) {
      const vars = parseVars(compileThemeVariables(theme))
      expect(vars['--code-bg'], `${theme.id} --code-bg 缺失`).toBeTruthy()
      for (const v of SYNTAX_VARS) {
        expect(
          parseColorToRgb(vars[v] ?? ''),
          `${theme.id} ${v} 应为主题分发的可解析色值`,
        ).toBeTruthy()
      }
    }
  })

  it('性质：底色亮 → 全部令牌对 --code-bg ≥ 4.5（AA）；底色暗 → oneDark 原生值', () => {
    for (const theme of Object.values(presetThemes)) {
      const vars = parseVars(compileThemeVariables(theme))
      const bg = parseColorToRgb(vars['--code-bg'])!
      if (luminance(bg) > 0.179) {
        for (const v of SYNTAX_VARS) {
          const ratio = contrast(parseColorToRgb(vars[v])!, bg)
          expect(ratio, `${theme.id} ${v} 对底色对比度 ${ratio.toFixed(2)} 不达 AA`).toBeGreaterThanOrEqual(4.5)
        }
      } else {
        for (const v of SYNTAX_VARS) {
          expect(vars[v], `${theme.id} 深底应沿用 oneDark 原生 ${v}`).toBe(ONE_DARK[v])
        }
      }
    }
  })

  it('分发按底色亮度而非主题 category：high-contrast（special 族、深底）拿深色调色板', () => {
    expect(presetThemes['high-contrast'].category).not.toBe('dark')
    const vars = parseVars(compileThemeVariables(presetThemes['high-contrast']))
    for (const v of SYNTAX_VARS) {
      expect(vars[v], `high-contrast ${v} 应为深色调色板`).toBe(ONE_DARK[v])
    }
  })

  it('防拟合：input 底色翻深/翻浅时令牌组随实际底色切换', () => {
    // 浅色主题 input 改深底 → 切深色组
    const light = presetThemes['light']
    const asDark = {
      ...light,
      colors: { ...light.colors, background: { ...light.colors.background, input: '#20242c' } },
    }
    const varsDark = parseVars(compileThemeVariables(asDark))
    expect(varsDark['--code-keyword']).toBe(ONE_DARK['--code-keyword'])

    // 深色族 --code-bg 钉死深底（兼容高亮配色），input 改亮不翻转底色与令牌组
    const dark = presetThemes['dark']
    const asLightInput = {
      ...dark,
      colors: { ...dark.colors, background: { ...dark.colors.background, input: '#f0f4ff' } },
    }
    const varsLight = parseVars(compileThemeVariables(asLightInput))
    expect(luminance(parseColorToRgb(varsLight['--code-bg'])!)).toBeLessThanOrEqual(0.179)
    expect(varsLight['--code-keyword']).toBe(ONE_DARK['--code-keyword'])
  })
})
