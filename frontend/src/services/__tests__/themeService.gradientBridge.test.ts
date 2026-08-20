/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * themeService — 渐变值进 shadcn hsl() 桥接的失效防护测试
 *
 * 真实缺陷（2026-08-20 用户报告"几个主题聊天/设置区有奇怪纹理+文字与背景同色"）：
 * deep-space/ocean-breeze/moe-soft 的 colors.background.main 是渐变字符串，
 * colorToHsl 原样输出后 hsl(var(--background)) 全线 invalid → 所有 bg-background
 * 面板透明 → body 全屏纹理层穿透内容区。chat 区同理：bg-[var(--chat-bg)] 是
 * background-color 位，渐变塞进去整条失效。
 */

import { describe, it, expect } from 'vitest'
import { compileThemeVariables } from '@/services/themeService'
import { deepSpaceTheme, darkTheme } from '@/config/themes'

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

/** hsl() 桥接位只接受 "H S% L%" 原始格式；渐变/其他原样字符串会让消费端全线失效 */
const HSL_RAW = /^\d+\s+\d+%\s+\d+%/

describe('shadcn hsl() 桥接 — 渐变主色不得原样输出', () => {
  it('deep-space（渐变 main）：--background 输出可解析的 HSL 原始值', () => {
    const vars = parseVars(compileThemeVariables(deepSpaceTheme))
    expect(HSL_RAW.test(vars['--background'])).toBe(true)
  })

  it('deep-space（渐变 main）：--card/--popover/--panel-solid 同样输出 HSL 原始值', () => {
    const vars = parseVars(compileThemeVariables(deepSpaceTheme))
    expect(HSL_RAW.test(vars['--card'])).toBe(true)
    expect(HSL_RAW.test(vars['--popover'])).toBe(true)
    expect(HSL_RAW.test(vars['--panel-solid'])).toBe(true)
  })

  it('纯色主题（dark）：桥接输出不受影响', () => {
    const vars = parseVars(compileThemeVariables(darkTheme))
    expect(HSL_RAW.test(vars['--background'])).toBe(true)
    // dark 的 --background 来自 #04060F，HSL ≈ 229 58% 4%
    expect(vars['--background'].startsWith('229 ')).toBe(true)
  })
})

describe('聊天区背景双通道 — 渐变进 image 位、纯色进 color 位', () => {
  it('deep-space（渐变 chat 背景）：--chat-bg-image 带渐变、--chat-bg-color 透明', () => {
    const vars = parseVars(compileThemeVariables(deepSpaceTheme))
    expect(vars['--chat-bg-image']).toContain('radial-gradient')
    expect(vars['--chat-bg-color']).toBe('transparent')
  })

  it('dark（纯色 chat 背景）：--chat-bg-image 为 none、--chat-bg-color 为纯色', () => {
    const vars = parseVars(compileThemeVariables(darkTheme))
    expect(vars['--chat-bg-image']).toBe('none')
    expect(vars['--chat-bg-color']).toBe('#04060F')
  })

  it('--chat-bg 原样保留（兼容既有消费端）', () => {
    const vars = parseVars(compileThemeVariables(darkTheme))
    expect(vars['--chat-bg']).toBe('#04060F')
  })
})
