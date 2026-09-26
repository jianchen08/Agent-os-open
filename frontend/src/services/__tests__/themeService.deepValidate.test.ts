/** @feature FP-T12 主题桥深校验 | @ci: frontend-test */
/**
 * validateThemeConfig v2 深校验（2026-09-25 桥协议统一波2）
 *
 * 行为契约（theme.apply 载荷经 compileThemeVariables 变 CSS 变量值注入宿主）：
 * - 值域本身是 CSS（语料实相：colors.bubble 有 radius/shadow 复合串、
 *   colors.background.main 有渐变）——不做颜色词法
 * - 全部字符串：长度上限 + 禁 url() + 禁绝对 http(s) + 禁 javascript:
 *   （远程外联探针/追踪像素/javascript: URI 封死；相对路径与 data: 可用）
 * - 防误伤语料锚：全部内置 preset 通过
 */

import { describe, expect, it } from 'vitest'
import { validateThemeConfig } from '../themeService'

const baseTheme = {
  id: 't',
  name: 'T',
  category: 'dark',
  colors: {
    primary: '#22D3EE',
    secondary: '#A78BFA',
    accent: '#F472B6',
    background: {
      main: 'linear-gradient(160deg, #fff9f5 0%, #fdf3ec 60%)',
      card: '#0A1226',
      sidebar: '#0B1428',
      input: '#0A1226',
      elevated: '#101C36',
    },
    text: { primary: '#F1F5F9', secondary: '#94A3B8', muted: '#64748B', disabled: '#475569' },
    border: { default: 'rgba(148, 163, 184, 0.12)', hover: '#334155', active: '#22D3EE' },
    status: {
      success: '#34D399',
      warning: '#FBBF24',
      error: '#F87171',
      info: '#60A5FA',
      running: '#60A5FA',
      pending: '#94A3B8',
    },
    bubble: {
      user_bg: '#1E293B',
      user_text: '#F1F5F9',
      user_radius: '1rem 1rem 1rem 0.25rem',
      user_shadow: '0 2px 8px rgba(34, 211, 238, 0.2)',
      ai_bg: '#0F172A',
      ai_text: '#E2E8F0',
    },
  },
  components: {
    card: { style: 'elevated' },
    borderRadius: { defaultRadius: 'md' },
    button: {
      hoverEffect: 'lift',
      variants: { secondary: { hoverBg: 'rgba(0, 0, 0, 0.2)', border: 'rgb(30, 41, 59)' } },
    },
  },
  effects: {
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.4, 0, 0.2, 1)',
    animations: true,
  },
  backgrounds: {
    main: { type: 'solid', value: '#04060F' },
    chat: { type: 'gradient', value: 'linear-gradient(180deg, #04060F, #0A1226)' },
    image: { enabled: false, url: '', position: 'center', size: 'cover' },
  },
}

describe('validateThemeConfig · 深校验', () => {
  it('合法载荷（含渐变/复合串的语料形态）通过', () => {
    expect(validateThemeConfig(baseTheme).valid).toBe(true)
  })

  it('全部内置 preset 通过（防误伤语料锚）', async () => {
    const { presetThemes } = await import('@/config/themes')
    const entries = Object.entries(presetThemes)
    expect(entries.length).toBeGreaterThan(0)
    for (const [id, config] of entries) {
      const result = validateThemeConfig(config)
      expect(result.errors ?? [], `preset ${id}`).toEqual([])
      expect(result.valid, `preset ${id} 应通过深校验`).toBe(true)
    }
  })

  it('colors 内 url() → 拒绝（CSS 变量值注入面封死）', () => {
    const bad = {
      ...baseTheme,
      colors: { ...baseTheme.colors, accent: 'url(https://evil.example/pixel)' },
    }
    const result = validateThemeConfig(bad)
    expect(result.valid).toBe(false)
    expect((result.errors ?? []).join()).toContain('禁止 url()')
  })

  it('backgrounds.image.url 绝对 http(s) 地址 → 拒绝（外链追踪像素封死）', () => {
    const bad = {
      ...baseTheme,
      backgrounds: {
        ...baseTheme.backgrounds,
        image: { ...baseTheme.backgrounds.image, url: 'https://evil.example/i.png' },
      },
    }
    const result = validateThemeConfig(bad)
    expect(result.valid).toBe(false)
    expect((result.errors ?? []).join()).toContain('http(s)')
  })

  it('超长字符串 → 拒绝（任意支柱，≥2 组区分输入）', () => {
    const long = 'x'.repeat(513)
    expect(
      validateThemeConfig({ ...baseTheme, effects: { ...baseTheme.effects, transitionEasing: long } })
        .valid,
    ).toBe(false)
    expect(
      validateThemeConfig({ ...baseTheme, colors: { ...baseTheme.colors, primary: long } }).valid,
    ).toBe(false)
  })

  it('数组载荷内的非法协议 → 深遍历递归仍拒绝', () => {
    const bad = {
      ...baseTheme,
      colors: { ...baseTheme.colors, palette: ['#101014', 'javascript:alert(1)'] },
    }
    const result = validateThemeConfig(bad)
    expect(result.valid).toBe(false)
    expect((result.errors ?? []).join()).toContain('非法协议')
  })

  it('javascript: 协议 → 拒绝', () => {
    const bad = {
      ...baseTheme,
      backgrounds: { ...baseTheme.backgrounds, main: { type: 'solid', value: 'javascript:alert(1)' } },
    }
    const result = validateThemeConfig(bad)
    expect(result.valid).toBe(false)
    expect((result.errors ?? []).join()).toContain('非法协议')
  })
})
