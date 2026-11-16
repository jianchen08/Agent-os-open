/** @feature: FP-0.2.双端优化R38 WebView110 color-mix 静态等价转换 | @ci: frontend-test */
/**
 * themeService — 皮肤令牌 color-mix 静态等价转换（WebView 110 兼容）
 *
 * 受损面（R36 遗留③取证，translator.skins_to_plugin_themes 16 皮全中）：
 * DSH 翻译器发射的皮肤令牌含 color-mix(in srgb, …) 派生面，Chromium <111
 * 不支持 color-mix → custom property 有值、var() 展开后的消费声明整体失效
 * （IACVT，声明级 fallback 不救）。修复 = applyPluginThemeVars 注入前把可
 * 静态解析形态转为等价 hex（parseColorMix 预乘 alpha 插值，与 111+ 浏览器
 * 渲染同式）；非 srgb 空间 / var() 引用 / 缺首百分比等不可解析形态原值透传。
 */

import { describe, expect, it, beforeEach } from 'vitest'
import { normalizeColorMix } from '@/services/backdropContrast'
import { applyPluginThemeVars, clearPluginThemeVars } from '@/services/themeService'
import type { PluginTheme } from '@/types/theme'

/** 翻译器真实发射的受损令牌形态（取证值：skins_to_plugin_themes 首皮样例） */
const TRANSLATOR_EMISSIONS: Array<[string, string]> = [
  ['--ds-bg-elevated', 'color-mix(in srgb, #e8ecf5 82%, white)'],
  ['--ds-bg-hover', 'color-mix(in srgb, #4a5fa8 12%, transparent)'],
  ['--ds-text-secondary', 'color-mix(in srgb, #1d2539 78%, #e8ecf5)'],
  ['--ds-text-muted', 'color-mix(in srgb, #1d2539 55%, #e8ecf5)'],
  ['--ds-border-subtle', 'color-mix(in srgb, #1d2539 16%, transparent)'],
  ['--bubble-ai-bg', 'color-mix(in srgb, #e8ecf5 80%, transparent)'],
]

describe('normalizeColorMix — 静态等价转换', () => {
  it('混 transparent：通道保持、alpha 乘百分比（#4a90d9 12% → #4a90d91f）', () => {
    expect(normalizeColorMix('color-mix(in srgb, #4a90d9 12%, transparent)')).toBe('#4a90d91f')
  })

  it('边界 0%：全透明（#00000000）', () => {
    expect(normalizeColorMix('color-mix(in srgb, #4a90d9 0%, transparent)')).toBe('#00000000')
  })

  it('边界 100%：不透明回 6 位 hex（与源色逐通道一致）', () => {
    expect(normalizeColorMix('color-mix(in srgb, #4a90d9 100%, transparent)')).toBe('#4a90d9')
  })

  it('非 srgb 空间不转换走原值', () => {
    const raw = 'color-mix(in oklab, #4a90d9 12%, transparent)'
    expect(normalizeColorMix(raw)).toBe(raw)
  })

  it.each([
    ['var() 引用', 'color-mix(in srgb, var(--accent) 12%, transparent)'],
    ['缺第二色', 'color-mix(in srgb, #4a90d9 12%)'],
    ['不可解析色', 'color-mix(in srgb, currentcolor 12%, transparent)'],
    ['残缺括号', 'color-mix(in srgb, #4a90d9 12%'],
    ['非 color-mix 值', '#4a90d9'],
    ['普通文本', 'transparent'],
  ])('非法输入跳过转换走原值（%s）', (_label, raw) => {
    expect(normalizeColorMix(raw)).toBe(raw)
  })

  it.each(TRANSLATOR_EMISSIONS)('翻译器受损令牌 %s 静态可转换', (_key, raw) => {
    const out = normalizeColorMix(raw)
    expect(out).toMatch(/^#[0-9a-f]{6}(?:[0-9a-f]{2})?$/)
    expect(out).not.toContain('color-mix')
  })

  it('混命名色 white：实色插值等价（--ds-bg-elevated 形态）', () => {
    // 232*0.82+255*0.18=236.14→0xec；236*0.82+45.9=239.42→0xef；245*0.82+45.9=246.8→0xf7
    expect(normalizeColorMix('color-mix(in srgb, #e8ecf5 82%, white)')).toBe('#eceff7')
  })

  it('双实色插值等价（--ds-text-secondary / muted 形态）', () => {
    expect(normalizeColorMix('color-mix(in srgb, #1d2539 78%, #e8ecf5)')).toBe('#4a5162')
    expect(normalizeColorMix('color-mix(in srgb, #1d2539 55%, #e8ecf5)')).toBe('#787f8e')
  })

  it('transparent 在首位：交换位不改变混色语义', () => {
    // a=0.6，通道 (255*0.6)/0.6=255 → #ff0000 + round(153)=0x99
    expect(normalizeColorMix('color-mix(in srgb, transparent 40%, #ff0000)')).toBe('#ff000099')
  })

  it('性质：alpha 随百分比单调不降，通道恒等于基色（全透明除外）', () => {
    const alphas: number[] = []
    for (const p of [0, 25, 50, 75, 100]) {
      const out = normalizeColorMix(`color-mix(in srgb, #336699 ${p}%, transparent)`)
      if (p === 0) {
        // 全透明：通道归零（parseColorMix outA≤0 分支），透明度语义不变
        expect(out).toBe('#00000000')
        alphas.push(0)
        continue
      }
      if (p === 100) {
        // 全不透明：回 6 位 hex（无 alpha 位）
        expect(out).toBe('#336699')
        alphas.push(255)
        continue
      }
      expect(out.startsWith('#336699')).toBe(true)
      alphas.push(parseInt(out.slice(7, 9), 16))
    }
    for (let i = 1; i < alphas.length; i++) {
      expect(alphas[i]).toBeGreaterThanOrEqual(alphas[i - 1])
    }
  })

  it('性质：幂等（转换结果再入原样返回）', () => {
    for (const [, raw] of TRANSLATOR_EMISSIONS) {
      const once = normalizeColorMix(raw)
      expect(normalizeColorMix(once)).toBe(once)
    }
  })

  it('性质：对映百分比同一混合（25% 黑 + 75% 白 = 75% 白 + 25% 黑）', () => {
    // 255*0.75=191.25→191=0xbf：同一物理混色从两侧声明，转换结果一致
    const fromBlack = normalizeColorMix('color-mix(in srgb, #000000 25%, #ffffff)')
    const fromWhite = normalizeColorMix('color-mix(in srgb, #ffffff 75%, #000000)')
    expect(fromBlack).toBe('#bfbfbf')
    expect(fromWhite).toBe(fromBlack)
  })
})

describe('applyPluginThemeVars — color-mix 注入钩路由', () => {
  beforeEach(() => {
    clearPluginThemeVars()
    document.documentElement.style.cssText = ''
  })

  function pluginTheme(variables: Record<string, string>): PluginTheme {
    return { id: 'dsh-skin-demo', name: 'demo', base: 'dark', pluginId: 'dsh_adapter', variables }
  }

  it('皮肤令牌注入前转为等价 hex（110 可消费）', () => {
    applyPluginThemeVars(pluginTheme(Object.fromEntries(TRANSLATOR_EMISSIONS)))
    expect(document.documentElement.style.getPropertyValue('--ds-bg-hover')).toBe('#4a5fa81f')
    expect(document.documentElement.style.getPropertyValue('--ds-border-subtle')).toBe('#1d253929')
    expect(document.documentElement.style.getPropertyValue('--bubble-ai-bg')).toBe('#e8ecf5cc')
    expect(document.documentElement.style.getPropertyValue('--ds-bg-elevated')).toBe('#eceff7')
  })

  it('不可静态解析形态原值透传（fail-open：110 维持现状、111+ 零变化）', () => {
    applyPluginThemeVars(
      pluginTheme({
        '--ds-text-secondary': 'color-mix(in oklab, #1d2539 78%, #e8ecf5)',
        '--ds-bg-panel': 'color-mix(in srgb, var(--x) 88%, white)',
      }),
    )
    expect(document.documentElement.style.getPropertyValue('--ds-text-secondary')).toBe(
      'color-mix(in oklab, #1d2539 78%, #e8ecf5)',
    )
    expect(document.documentElement.style.getPropertyValue('--ds-bg-panel')).toBe(
      'color-mix(in srgb, var(--x) 88%, white)',
    )
  })

  it('普通值不经转换，HSL 桥归一化不受影响', () => {
    applyPluginThemeVars(
      pluginTheme({
        '--ds-bg-canvas': '#111318',
        '--region-sidebar-bg': '#123456',
        '--region-sidebar-muted-fg': '#abafbd',
      }),
    )
    expect(document.documentElement.style.getPropertyValue('--ds-bg-canvas')).toBe('#111318')
    expect(document.documentElement.style.getPropertyValue('--region-sidebar-bg')).toBe('#123456')
    const muted = document.documentElement.style.getPropertyValue('--region-sidebar-muted-fg')
    expect(muted).not.toContain('#')
    expect(muted.split(' ')).toHaveLength(3)
  })
})
