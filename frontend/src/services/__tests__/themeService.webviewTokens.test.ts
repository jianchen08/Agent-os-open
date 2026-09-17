/** @feature: FP-0.2.四 前端 Schema（面板-宿主融合） | @ci: frontend-test */
/**
 * themeService — buildWebviewThemeTokens（theme.sync 下行载荷构造）单测
 *
 * 协议固定 12 键（--ag-*）：
 * - 颜色优先取生效 ThemeConfig 声明值（四支柱：colors/components）
 * - 配置缺省键回退宿主 documentElement 实际 CSS 变量值
 * - --ag-accent-soft 由 accent 派生半透明浅底
 * - pluginVarOverlay（插件皮肤变量）只覆盖命中的键
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { presetThemes } from '@/config/themes'
import { buildWebviewThemeTokens } from '@/services/webviewThemeTokens'
import type { ThemeConfig } from '@/types/theme'

const DARK = presetThemes['dark'] as ThemeConfig

const TOKEN_KEYS = [
  '--ag-bg',
  '--ag-fg',
  '--ag-muted',
  '--ag-border',
  '--ag-card',
  '--ag-accent',
  '--ag-accent-soft',
  '--ag-chip',
  '--ag-ok',
  '--ag-warn',
  '--ag-err',
  '--ag-radius',
]

beforeEach(() => {
  document.documentElement.style.removeProperty('--bg-main')
})

describe('buildWebviewThemeTokens — 声明值优先', () => {
  it('完整 ThemeConfig → 固定 12 键全非空，颜色取声明值', () => {
    const tokens = buildWebviewThemeTokens(DARK)
    expect(Object.keys(tokens).sort()).toEqual([...TOKEN_KEYS].sort())
    for (const key of TOKEN_KEYS) expect(tokens[key].length).toBeGreaterThan(0)
    expect(tokens['--ag-bg']).toBe(DARK.colors.background.main)
    expect(tokens['--ag-fg']).toBe(DARK.colors.text.primary)
    expect(tokens['--ag-muted']).toBe(DARK.colors.text.secondary)
    expect(tokens['--ag-border']).toBe(DARK.colors.border.default)
    expect(tokens['--ag-card']).toBe(DARK.colors.background.card)
    expect(tokens['--ag-accent']).toBe(DARK.colors.accent)
    expect(tokens['--ag-chip']).toBe(DARK.components.badge.variants.default.bg)
    expect(tokens['--ag-ok']).toBe(DARK.colors.status.success)
    expect(tokens['--ag-warn']).toBe(DARK.colors.status.warning)
    expect(tokens['--ag-err']).toBe(DARK.colors.status.error)
    expect(tokens['--ag-radius']).toBe(DARK.components.borderRadius[DARK.components.borderRadius.defaultRadius])
  })

  it('--ag-accent-soft 由 accent 派生半透明浅底（hex 与 rgba 两组输入）', () => {
    const tokens = buildWebviewThemeTokens(DARK)
    // dark accent #A78BFA → rgba(167, 139, 250, 0.14)
    expect(tokens['--ag-accent-soft']).toBe('rgba(167, 139, 250, 0.14)')

    const rgbaAccent = { ...DARK, colors: { ...DARK.colors, accent: 'rgb(16, 185, 129)' } }
    expect(buildWebviewThemeTokens(rgbaAccent)['--ag-accent-soft']).toBe('rgba(16, 185, 129, 0.14)')
  })
})

describe('buildWebviewThemeTokens — 缺省回退', () => {
  it('config 为 null → 回退宿主 documentElement 实际 CSS 变量值', () => {
    document.documentElement.style.setProperty('--bg-main', '#0B0F1A')
    document.documentElement.style.setProperty('--text-primary', '#E2E8F0')
    const tokens = buildWebviewThemeTokens(null)
    expect(tokens['--ag-bg']).toBe('#0B0F1A')
    expect(tokens['--ag-fg']).toBe('#E2E8F0')
    document.documentElement.style.removeProperty('--bg-main')
    document.documentElement.style.removeProperty('--text-primary')
  })

  it('声明值存在时不读 CSS 变量（声明值优先于宿主已落地值）', () => {
    document.documentElement.style.setProperty('--bg-main', '#0B0F1A')
    const tokens = buildWebviewThemeTokens(DARK)
    expect(tokens['--ag-bg']).toBe(DARK.colors.background.main)
    document.documentElement.style.removeProperty('--bg-main')
  })
})

describe('buildWebviewThemeTokens — 插件变量 overlay', () => {
  it('overlay 只覆盖命中的键，未声明键保持配置值', () => {
    const tokens = buildWebviewThemeTokens(DARK, {
      '--ag-accent': '#FF8800',
      '--ds-unrelated': '#000000',
    })
    expect(tokens['--ag-accent']).toBe('#FF8800')
    expect(tokens['--ag-bg']).toBe(DARK.colors.background.main)
  })
})
