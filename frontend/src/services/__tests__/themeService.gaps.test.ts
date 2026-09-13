// @feature: FP-T12 themeService 缺口补测 | @ci: frontend-test
/**
 * themeService 缺口补测：编译器可选分支 / applyTheme 渐变背景 / 插件纹理形态 /
 * fetchDynamicThemes 空集容错 / validateThemeConfig 校验矩阵 / shadcn 桥接容错。
 *
 * 行为契约均以公开 API 的输入→输出断言，不触私有实现。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import {
  applyPluginThemeVars,
  applyTheme,
  compileThemeVariables,
  fetchDynamicThemes,
  getPresetTheme,
  validateThemeConfig,
} from '@/services/themeService'
import type { PluginTheme, ThemeConfig } from '@/types/theme'

const { importTheme } = vi.hoisted(() => ({ importTheme: vi.fn() }))
vi.mock('@/services/themeStorage', () => ({
  ThemeStorageService: { importTheme },
}))

const dark = presetThemes['dark']

function withColors(patch: Partial<ThemeConfig['colors']>): ThemeConfig {
  return { ...dark, colors: { ...dark.colors, ...patch } }
}

function withBubble(patch: Partial<ThemeConfig['colors']['bubble']>): ThemeConfig {
  return withColors({ bubble: { ...dark.colors.bubble, ...patch } })
}

function pluginTheme(overrides: Partial<PluginTheme> = {}): PluginTheme {
  return {
    id: 'gold-lace',
    name: '金色蕾丝',
    base: 'dark',
    pluginId: 'demo_plugin',
    ...overrides,
  }
}

describe('getPresetTheme — 注册点查找', () => {
  it('已注册 id 返回预设配置，未注册 id 返回 null', () => {
    expect(getPresetTheme('dark')?.id).toBe('dark')
    expect(getPresetTheme('no-such-theme')).toBeNull()
  })
})

describe('compileThemeVariables — 气泡可选形态字段', () => {
  it('user_padding / ai_padding 声明时发射对应变量', () => {
    const vars = compileThemeVariables(
      withBubble({ user_padding: '12px 16px', ai_padding: '8px 12px' }),
    )
    expect(vars).toContain('--bubble-user-padding: 12px 16px')
    expect(vars).toContain('--bubble-ai-padding: 8px 12px')
  })

  it('padding 未声明时不发射该变量（CSS 端走缺省值）', () => {
    const vars = compileThemeVariables(dark)
    expect(vars).not.toContain('--bubble-user-padding:')
    expect(vars).not.toContain('--bubble-ai-padding:')
  })
})

describe('compileThemeVariables — 渐变气泡面取实色派生链接色', () => {
  it('气泡面为含 hex 色标的渐变：从色标提取实色，--bubble-link 仍发射', () => {
    const vars = compileThemeVariables(
      withBubble({ user_bg: 'linear-gradient(180deg, #0B1220 0%, #1E293B 100%)' }),
    )
    const link = vars.match(/--bubble-link: (#[0-9a-fA-F]{6})/)
    expect(link).not.toBeNull()
    // 链接色可发射为合法 hex 即证明实色提取通道工作（黑白兜底或品牌主色，
    // 择优契约由 bubbleLink 契约测试锁定）
    expect(['#000000', '#ffffff', dark.colors.primary.toLowerCase()]).toContain(link![1].toLowerCase())
  })

  it('气泡面渐变无可解析色标：不发射 --bubble-link（CSS 回退 hsl(--primary)）', () => {
    const vars = compileThemeVariables(
      withBubble({ user_bg: 'linear-gradient(to right, red, blue)' }),
    )
    expect(vars).not.toContain('--bubble-link:')
  })
})

describe('applyTheme — 背景主背景双通道', () => {
  beforeEach(() => {
    document.documentElement.style.cssText = ''
    document.body.style.background = ''
  })

  it('main 为渐变：变量走 --bg-main-gradient，body 背景同步渐变值', () => {
    // 用 rgb() 形态输入，避开 jsdom 对 hex 的颜色归一化，聚焦「值透传」语义
    const gradient = 'linear-gradient(180deg, rgb(4, 6, 15) 0%, rgb(10, 18, 38) 100%)'
    applyTheme({
      ...dark,
      backgrounds: { ...dark.backgrounds, main: { type: 'gradient', value: gradient } },
    })
    expect(document.documentElement.style.getPropertyValue('--bg-main-gradient')).toBe(gradient)
    expect(document.body.style.background).toBe(gradient)
  })

  it('main 为纯色：--bg-main-gradient 置 none，body 背景为纯色值', () => {
    applyTheme({
      ...dark,
      backgrounds: { ...dark.backgrounds, main: { type: 'solid', value: 'rgb(18, 52, 86)' } },
    })
    expect(document.documentElement.style.getPropertyValue('--bg-main-gradient')).toBe('none')
    expect(document.body.style.background).toBe('rgb(18, 52, 86)')
  })

  it('浅色主题：root 挂 light 类（Tailwind dark 模式类名切换）', () => {
    applyTheme(presetThemes['light'])
    expect(document.documentElement.classList.contains('light')).toBe(true)
    expect(document.documentElement.classList.contains('dark')).toBe(false)
  })
})

describe('applyPluginThemeVars — 纹理形态', () => {
  beforeEach(() => {
    document.documentElement.style.cssText = ''
  })

  it.each([
    ['grid', 'linear-gradient(90deg'],
    ['lines', 'repeating-linear-gradient'],
    ['checker', 'repeating-conic-gradient'],
  ] as const)('texture.type=%s 生成对应纹理 CSS', (type, marker) => {
    applyPluginThemeVars(
      pluginTheme({ backgrounds: { texture: { enabled: true, type, size: '32px' } } }),
    )
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toContain(marker)
  })

  it.each(['none', 'noise'] as const)('texture.type=%s 纹理置 none（显式关闭/无生成器）', (type) => {
    applyPluginThemeVars(pluginTheme({ backgrounds: { texture: { enabled: true, type } } }))
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
  })

  it('image 全量声明：position/size/attachment/overlay/overlayOpacity 逐项落变量', () => {
    applyPluginThemeVars(
      pluginTheme({
        backgrounds: {
          image: {
            enabled: true,
            url: 'https://x.example/bg.png',
            position: 'center top',
            size: 'cover',
            attachment: 'fixed',
            overlay: 'rgba(0, 0, 0, 0.6)',
            overlayOpacity: 0.6,
          },
        },
      }),
    )
    const style = document.documentElement.style
    expect(style.getPropertyValue('--bg-image')).toBe('url(https://x.example/bg.png)')
    expect(style.getPropertyValue('--bg-image-position')).toBe('center top')
    expect(style.getPropertyValue('--bg-image-size')).toBe('cover')
    expect(style.getPropertyValue('--bg-image-attachment')).toBe('fixed')
    expect(style.getPropertyValue('--bg-overlay')).toBe('rgba(0, 0, 0, 0.6)')
    expect(style.getPropertyValue('--bg-overlay-opacity')).toBe('0.6')
  })

  it('texture.opacity 声明时发射 --bg-texture-opacity', () => {
    applyPluginThemeVars(
      pluginTheme({ backgrounds: { texture: { enabled: true, type: 'dots', opacity: 0.4 } } }),
    )
    expect(document.documentElement.style.getPropertyValue('--bg-texture-opacity')).toBe('0.4')
  })
})

describe('fetchDynamicThemes — 无可发现主题', () => {
  it('glob 空集时直接返回：不导入、不告警', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    await fetchDynamicThemes()
    expect(importTheme).not.toHaveBeenCalled()
    expect(warnSpy).not.toHaveBeenCalled()
    warnSpy.mockRestore()
  })
})

describe('validateThemeConfig — 校验矩阵', () => {
  const validConfig = {
    id: 't',
    name: 'T',
    colors: {
      primary: '#000000',
      secondary: '#111111',
      accent: '#222222',
      background: { main: '#000000' },
      text: { primary: '#ffffff' },
      border: { default: '#333333' },
    },
    components: {},
    effects: {},
    backgrounds: {},
  }

  it.each([
    ['null', null],
    ['字符串', 'not-an-object'],
    ['数字', 42],
  ])('非对象输入（%s）→ invalid，错误为「配置不是对象」', (_label, input) => {
    expect(validateThemeConfig(input)).toEqual({ valid: false, errors: ['配置不是对象'] })
  })

  it('空对象：六个顶层缺失错误一次报全', () => {
    const result = validateThemeConfig({})
    expect(result.valid).toBe(false)
    expect(result.errors).toEqual([
      '缺少或无效的 id 字段',
      '缺少或无效的 name 字段',
      '缺少或无效的 colors 字段',
      '缺少或无效的 components 字段',
      '缺少或无效的 effects 字段',
      '缺少或无效的 backgrounds 字段',
    ])
  })

  it.each(['id', 'name', 'colors', 'components', 'effects', 'backgrounds'] as const)(
    '仅缺 %s 时报对应缺失错误',
    (field) => {
      const config: Record<string, unknown> = { ...validConfig }
      delete config[field]
      const result = validateThemeConfig(config)
      expect(result.valid).toBe(false)
      expect(result.errors).toContain(`缺少或无效的 ${field} 字段`)
    },
  )

  it.each(['primary', 'secondary', 'accent', 'background', 'text', 'border'] as const)(
    'colors 缺 %s 时报「缺少必需的颜色字段」',
    (field) => {
      const colors: Record<string, unknown> = { ...validConfig.colors }
      delete colors[field]
      const result = validateThemeConfig({ ...validConfig, colors })
      expect(result.valid).toBe(false)
      expect(result.errors).toContain(`缺少必需的颜色字段: ${field}`)
    },
  )

  it('完整最小配置 → valid 且无错误列表', () => {
    expect(validateThemeConfig(validConfig)).toEqual({ valid: true, errors: undefined })
  })
})

describe('compileThemeVariables — shadcn 桥接容错', () => {
  it('无法解析的颜色值经 colorToHsl 原样透传（不产生 NaN 桥接值）', () => {
    const vars = compileThemeVariables(withColors({ text: { ...dark.colors.text, primary: 'not-a-color' } }))
    expect(vars).toContain('--foreground: not-a-color')
    expect(vars).not.toMatch(/--foreground: [^ ]*NaN/)
  })

  it('background.elevated 为渐变：--panel-solid 提取色标转实色 HSL（不带 hsl() 包裹、不含渐变原文）', () => {
    const vars = compileThemeVariables(
      withColors({
        background: { ...dark.colors.background, elevated: 'linear-gradient(90deg, #112233, #445566)' },
      }),
    )
    const panel = vars.match(/--panel-solid: (\d+ \d+% \d+%)/)
    expect(panel).not.toBeNull()
    expect(vars).not.toContain('--panel-solid: linear-gradient')
  })

  it('background.elevated 无法解析：--panel-solid 原样透传', () => {
    const vars = compileThemeVariables(
      withColors({ background: { ...dark.colors.background, elevated: 'mystery-paint' } }),
    )
    expect(vars).toContain('--panel-solid: mystery-paint')
  })
})
