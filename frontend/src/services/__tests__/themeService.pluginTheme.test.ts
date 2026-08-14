/**
 * themeService — 插件主题（contributes.themes）应用测试
 *
 * 覆盖 task_plugin_frontend_customization.md 任务 1：
 * - applyPluginThemeVars：变量 setProperty 覆盖 / 背景 enabled 开关语义
 * - derivePluginThemePreview：从 --ds-* 变量派生预览色，缺省回退 base 主题
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { applyPluginThemeVars, derivePluginThemePreview } from '@/services/themeService'
import type { PluginTheme } from '@/types/theme'

function pluginTheme(overrides: Partial<PluginTheme> = {}): PluginTheme {
  return {
    id: 'gold-lace',
    name: '金色蕾丝',
    base: 'dark',
    pluginId: 'demo_plugin',
    variables: { '--ds-accent-primary': '#D4AF37' },
    ...overrides,
  }
}

describe('applyPluginThemeVars — 变量与背景覆盖', () => {
  beforeEach(() => {
    document.documentElement.style.cssText = ''
    document.body.classList.remove('has-bg-image')
    document.documentElement.style.removeProperty('--bg-image')
    document.documentElement.style.removeProperty('--bg-texture')
  })

  it('声明的变量逐个 setProperty（覆盖 base 主题值）', () => {
    applyPluginThemeVars(
      pluginTheme({
        variables: {
          '--ds-accent-primary': '#D4AF37',
          '--ds-bg-panel': 'rgba(40, 35, 20, 0.9)',
        },
      }),
    )
    expect(document.documentElement.style.getPropertyValue('--ds-accent-primary')).toBe('#D4AF37')
    expect(document.documentElement.style.getPropertyValue('--ds-bg-panel')).toBe('rgba(40, 35, 20, 0.9)')
  })

  it('无 variables 时不做任何 setProperty（不崩溃）', () => {
    applyPluginThemeVars(pluginTheme({ variables: undefined }))
    expect(document.documentElement.style.cssText).toBe('')
  })

  it('image.enabled=false 显式关闭宿主背景图片', () => {
    document.body.classList.add('has-bg-image')
    document.documentElement.style.setProperty('--bg-image', 'url(host-bg.png)')
    applyPluginThemeVars(pluginTheme({ backgrounds: { image: { enabled: false } } }))
    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    expect(document.documentElement.style.getPropertyValue('--bg-image')).toBe('')
  })

  it('image 声明 url 时按配置覆盖背景图片', () => {
    applyPluginThemeVars(
      pluginTheme({
        backgrounds: {
          image: { enabled: true, url: 'https://x.example/gold.png', position: 'center' },
        },
      }),
    )
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
    expect(document.documentElement.style.getPropertyValue('--bg-image')).toBe('url(https://x.example/gold.png)')
    expect(document.documentElement.style.getPropertyValue('--bg-image-position')).toBe('center')
  })

  it('texture.enabled=false 或 type=none → 关闭纹理', () => {
    document.documentElement.style.setProperty('--bg-texture', 'radial-gradient(...)')
    applyPluginThemeVars(pluginTheme({ backgrounds: { texture: { enabled: false } } }))
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('none')
  })

  it('texture 声明 type 时生成纹理 CSS', () => {
    applyPluginThemeVars(
      pluginTheme({ backgrounds: { texture: { enabled: true, type: 'dots', color: 'rgba(212,175,55,0.5)' } } }),
    )
    const value = document.documentElement.style.getPropertyValue('--bg-texture')
    expect(value).toContain('radial-gradient')
    expect(value).toContain('rgba(212,175,55,0.5)')
  })
})

describe('derivePluginThemePreview — 预览色派生', () => {
  it('优先取声明的 --ds-* 变量', () => {
    const preview = derivePluginThemePreview(
      pluginTheme({
        variables: {
          '--ds-accent-primary': '#D4AF37',
          '--ds-bg-canvas': '#17120A',
          '--ds-bg-panel': '#2B2413',
          '--ds-text-primary': '#F5EECB',
          '--ds-accent-ai': '#E8C56A',
        },
      }),
    )
    expect(preview).toEqual({
      primary: '#D4AF37',
      background: '#17120A',
      surface: '#2B2413',
      text: '#F5EECB',
      accent: '#E8C56A',
    })
  })

  it('缺省回退 base 主题预览色（dark/light）', () => {
    const dark = derivePluginThemePreview(pluginTheme({ base: 'dark', variables: undefined }))
    expect(dark?.primary).toBeTruthy()
    expect(dark?.background).toBeTruthy()

    const light = derivePluginThemePreview(pluginTheme({ base: 'light', variables: undefined }))
    expect(light?.primary).toBeTruthy()
  })
})
