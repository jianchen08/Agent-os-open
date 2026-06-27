/**
 * 主题服务
 *
 * 提供主题应用、合并等工具函数
 * 主题配置由前端管理：预设主题打包进 bundle，动态主题通过后端无状态清单
 * （/api/v1/themes/manifest）发现后 fetch JSON 存入 localStorage。
 */

import { API_ENDPOINTS } from '@/constants/api'
import {
  darkTheme,
  lightTheme,
  deepSpaceTheme,
  oceanBreezeTheme,
  highContrastTheme,
} from '@/config/themes'
import apiClient from '@/services/api/client'
import { ThemeStorageService, mergeTheme as mergeUserTheme } from '@/services/themeStorage'
import type { ThemeConfig } from '@/types/theme'

/**
 * 获取预设主题配置
 *
 * @param themeId - 主题 ID
 * @returns 主题配置，如果不存在则返回 null
 */
export function getPresetTheme(themeId: string): ThemeConfig | null {
  const presetThemes: Record<string, ThemeConfig> = {
    dark: darkTheme,
    light: lightTheme,
    'deep-space': deepSpaceTheme,
    'ocean-breeze': oceanBreezeTheme,
    'high-contrast': highContrastTheme,
  }
  return presetThemes[themeId] || null
}

/**
 * 获取所有预设主题
 *
 * @returns 预设主题列表
 */
export function getAllPresetThemes(): ThemeConfig[] {
  return [darkTheme, lightTheme]
}

/**
 * 编译主题配置为 CSS 变量字符串
 *
 * 将主题配置转换为 CSS 变量声明，用于批量设置到 DOM
 *
 * @param config - 主题配置
 * @returns CSS 变量字符串
 */
export function compileThemeVariables(config: ThemeConfig): string {
  const vars: string[] = []

  // === 基础颜色 ===
  vars.push(`--primary: ${config.colors.primary}`)
  vars.push(`--secondary: ${config.colors.secondary}`)
  vars.push(`--accent: ${config.colors.accent}`)

  // === 选中态颜色（基于 primary 色动态生成）===
  const primaryRgb = hexToRgb(config.colors.primary)
  if (primaryRgb) {
    const isDark = config.category === 'dark'
    vars.push(`--selection-bg: rgba(${primaryRgb.r}, ${primaryRgb.g}, ${primaryRgb.b}, ${isDark ? 0.35 : 0.25})`)
    vars.push(`--selection-text: ${isDark ? '#ffffff' : 'inherit'}`)
  }

  // === 背景色 ===
  Object.entries(config.colors.background).forEach(([key, value]) => {
    vars.push(`--bg-${kebabCase(key)}: ${value}`)
  })

  // === 文字色 ===
  Object.entries(config.colors.text).forEach(([key, value]) => {
    vars.push(`--text-${kebabCase(key)}: ${value}`)
  })

  // === 边框色 ===
  Object.entries(config.colors.border).forEach(([key, value]) => {
    vars.push(`--border-${kebabCase(key)}: ${value}`)
  })

  // === 状态色 ===
  Object.entries(config.colors.status).forEach(([key, value]) => {
    vars.push(`--status-${kebabCase(key)}: ${value}`)
  })

  // === 消息气泡 ===
  vars.push(`--bubble-user-bg: ${config.colors.bubble.user_bg}`)
  vars.push(`--bubble-user-text: ${config.colors.bubble.user_text}`)
  vars.push(`--bubble-ai-bg: ${config.colors.bubble.ai_bg}`)
  vars.push(`--bubble-ai-text: ${config.colors.bubble.ai_text}`)
  if (config.colors.bubble.user_radius) {
    vars.push(`--bubble-user-radius: ${config.colors.bubble.user_radius}`)
  }
  if (config.colors.bubble.user_shadow) {
    vars.push(`--bubble-user-shadow: ${config.colors.bubble.user_shadow}`)
  }
  if (config.colors.bubble.user_border) {
    vars.push(`--bubble-user-border: ${config.colors.bubble.user_border}`)
  }
  if (config.colors.bubble.user_padding) {
    vars.push(`--bubble-user-padding: ${config.colors.bubble.user_padding}`)
  }
  if (config.colors.bubble.ai_radius) {
    vars.push(`--bubble-ai-radius: ${config.colors.bubble.ai_radius}`)
  }
  if (config.colors.bubble.ai_shadow) {
    vars.push(`--bubble-ai-shadow: ${config.colors.bubble.ai_shadow}`)
  }
  if (config.colors.bubble.ai_border) {
    vars.push(`--bubble-ai-border: ${config.colors.bubble.ai_border}`)
  }
  if (config.colors.bubble.ai_padding) {
    vars.push(`--bubble-ai-padding: ${config.colors.bubble.ai_padding}`)
  }

  // === 组件样式：按钮 ===
  if (config.components.button?.variants) {
    const variants = config.components.button.variants

    // Primary 按钮
    if (variants.primary) {
      vars.push(`--btn-primary-bg: ${variants.primary.bg}`)
      vars.push(`--btn-primary-text: ${variants.primary.text}`)
      vars.push(`--btn-primary-border: ${variants.primary.border}`)
      if (variants.primary.hoverBg) {
        vars.push(`--btn-primary-hover-bg: ${variants.primary.hoverBg}`)
      }
    }

    // Secondary 按钮
    if (variants.secondary) {
      vars.push(`--btn-secondary-bg: ${variants.secondary.bg}`)
      vars.push(`--btn-secondary-text: ${variants.secondary.text}`)
      vars.push(`--btn-secondary-border: ${variants.secondary.border}`)
      if (variants.secondary.hoverBg) {
        vars.push(`--btn-secondary-hover-bg: ${variants.secondary.hoverBg}`)
      }
    }

    // Ghost 按钮
    if (variants.ghost) {
      vars.push(`--btn-ghost-bg: ${variants.ghost.bg}`)
      vars.push(`--btn-ghost-text: ${variants.ghost.text}`)
      vars.push(`--btn-ghost-border: ${variants.ghost.border}`)
      if (variants.ghost.hoverBg) {
        vars.push(`--btn-ghost-hover-bg: ${variants.ghost.hoverBg}`)
      }
    }

    // Destructive 按钮
    if (variants.destructive) {
      vars.push(`--btn-destructive-bg: ${variants.destructive.bg}`)
      vars.push(`--btn-destructive-text: ${variants.destructive.text}`)
      vars.push(`--btn-destructive-border: ${variants.destructive.border}`)
      if (variants.destructive.hoverBg) {
        vars.push(`--btn-destructive-hover-bg: ${variants.destructive.hoverBg}`)
      }
    }
  }

  // === 按钮额外样式 ===
  if (config.components.button) {
    // 圆角样式：rounded/square/pill
    const styleRadiusMap: Record<string, string> = {
      rounded: '0.5rem',
      square: '0.125rem',
      pill: '9999px',
    }
    const btnRadius = styleRadiusMap[config.components.button.style] || '0.5rem'
    vars.push(`--btn-radius: ${btnRadius}`)

    // 阴影
    if (config.components.button.shadow) {
      vars.push(`--btn-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1)`)
      vars.push(`--btn-shadow-hover: 0 6px 10px -1px rgba(0, 0, 0, 0.15)`)
    } else {
      vars.push(`--btn-shadow: none`)
      vars.push(`--btn-shadow-hover: none`)
    }

    // 悬停效果
    vars.push(`--btn-hover-effect: ${config.components.button.hoverEffect}`)
  }

  // === 组件样式：输入框 ===
  if (config.components.input) {
    if (config.components.input.focusBorder) {
      vars.push(`--input-focus-border: ${config.components.input.focusBorder}`)
    }
    if (config.components.input.focusGlow) {
      vars.push(`--input-focus-ring: ${config.components.input.focusGlow}`)
    }
    // 输入框样式：filled/outlined/underline
    if (config.components.input.style) {
      vars.push(`--input-style: ${config.components.input.style}`)
    }
  }

  // 输入框背景色（从 background.input 获取）
  if (config.colors.background.input) {
    vars.push(`--bg-input: ${config.colors.background.input}`)
  }

  // === 组件样式：卡片 ===
  if (config.components.card) {
    if (config.components.card.border) {
      vars.push(`--card-border: ${config.components.card.border}`)
    }
    if (config.components.card.blur) {
      vars.push(`--card-backdrop-blur: ${config.components.card.blur}`)
    }
  }

  // === 组件样式：徽章 ===
  if (config.components.badge) {
    vars.push(`--badge-radius: ${config.components.badge.borderRadius}`)
    if (config.components.badge.variants) {
      const variants = config.components.badge.variants
      if (variants.default) {
        vars.push(`--badge-default-bg: ${variants.default.bg}`)
        vars.push(`--badge-default-text: ${variants.default.text}`)
        vars.push(`--badge-default-border: ${variants.default.border}`)
      }
      if (variants.secondary) {
        vars.push(`--badge-secondary-bg: ${variants.secondary.bg}`)
        vars.push(`--badge-secondary-text: ${variants.secondary.text}`)
        vars.push(`--badge-secondary-border: ${variants.secondary.border}`)
      }
      if (variants.success) {
        vars.push(`--badge-success-bg: ${variants.success.bg}`)
        vars.push(`--badge-success-text: ${variants.success.text}`)
        vars.push(`--badge-success-border: ${variants.success.border}`)
      }
      if (variants.warning) {
        vars.push(`--badge-warning-bg: ${variants.warning.bg}`)
        vars.push(`--badge-warning-text: ${variants.warning.text}`)
        vars.push(`--badge-warning-border: ${variants.warning.border}`)
      }
      if (variants.error) {
        vars.push(`--badge-error-bg: ${variants.error.bg}`)
        vars.push(`--badge-error-text: ${variants.error.text}`)
        vars.push(`--badge-error-border: ${variants.error.border}`)
      }
      if (variants.info) {
        vars.push(`--badge-info-bg: ${variants.info.bg}`)
        vars.push(`--badge-info-text: ${variants.info.text}`)
        vars.push(`--badge-info-border: ${variants.info.border}`)
      }
    }
  }

  // === 组件样式：对话框 ===
  if (config.components.dialog) {
    vars.push(`--dialog-radius: ${config.components.dialog.borderRadius}`)
    vars.push(`--dialog-overlay-bg: ${config.components.dialog.overlayBg}`)
    vars.push(`--dialog-overlay-opacity: ${config.components.dialog.overlayOpacity}`)
    vars.push(`--dialog-shadow: ${config.components.dialog.shadow}`)
    vars.push(`--dialog-border: ${config.components.dialog.border}`)
  }

  // === 组件样式：标签页 ===
  if (config.components.tabs) {
    vars.push(`--tabs-radius: ${config.components.tabs.borderRadius}`)
    vars.push(`--tabs-list-bg: ${config.components.tabs.listBg}`)
    vars.push(`--tabs-active-bg: ${config.components.tabs.activeBg}`)
    vars.push(`--tabs-active-text: ${config.components.tabs.activeText}`)
    vars.push(`--tabs-inactive-text: ${config.components.tabs.inactiveText}`)
  }

  // === 组件样式：Toast ===
  if (config.components.toast) {
    vars.push(`--toast-radius: ${config.components.toast.borderRadius}`)
    vars.push(`--toast-shadow: ${config.components.toast.shadow}`)
    if (config.components.toast.variants) {
      const variants = config.components.toast.variants
      if (variants.default) {
        vars.push(`--toast-default-bg: ${variants.default.bg}`)
        vars.push(`--toast-default-text: ${variants.default.text}`)
        vars.push(`--toast-default-border: ${variants.default.border}`)
      }
      if (variants.success) {
        vars.push(`--toast-success-bg: ${variants.success.bg}`)
        vars.push(`--toast-success-text: ${variants.success.text}`)
        vars.push(`--toast-success-border: ${variants.success.border}`)
      }
      if (variants.error) {
        vars.push(`--toast-error-bg: ${variants.error.bg}`)
        vars.push(`--toast-error-text: ${variants.error.text}`)
        vars.push(`--toast-error-border: ${variants.error.border}`)
      }
      if (variants.warning) {
        vars.push(`--toast-warning-bg: ${variants.warning.bg}`)
        vars.push(`--toast-warning-text: ${variants.warning.text}`)
        vars.push(`--toast-warning-border: ${variants.warning.border}`)
      }
      if (variants.info) {
        vars.push(`--toast-info-bg: ${variants.info.bg}`)
        vars.push(`--toast-info-text: ${variants.info.text}`)
        vars.push(`--toast-info-border: ${variants.info.border}`)
      }
    }
  }

  // === 组件样式：进度条 ===
  if (config.components.progress) {
    vars.push(`--progress-radius: ${config.components.progress.borderRadius}`)
    vars.push(`--progress-track-bg: ${config.components.progress.trackBg}`)
    if (config.components.progress.variants) {
      vars.push(`--progress-default: ${config.components.progress.variants.default}`)
      vars.push(`--progress-success: ${config.components.progress.variants.success}`)
      vars.push(`--progress-warning: ${config.components.progress.variants.warning}`)
      vars.push(`--progress-error: ${config.components.progress.variants.error}`)
    }
  }

  // === 组件样式：下拉菜单 ===
  if (config.components.dropdownMenu) {
    vars.push(`--dropdown-radius: ${config.components.dropdownMenu.borderRadius}`)
    vars.push(`--dropdown-shadow: ${config.components.dropdownMenu.shadow}`)
    vars.push(`--dropdown-border: ${config.components.dropdownMenu.border}`)
    vars.push(`--dropdown-item-hover-bg: ${config.components.dropdownMenu.itemHoverBg}`)
    vars.push(`--dropdown-item-hover-text: ${config.components.dropdownMenu.itemHoverText}`)
  }

  // === 发光效果 ===
  if (config.components.glow) {
    Object.entries(config.components.glow).forEach(([key, value]) => {
      if (key !== 'defaultGlowIntensity') {
        vars.push(`--status-${kebabCase(key)}-shadow: ${value}`)
      }
    })
  }

  // === 圆角 ===
  if (config.components.borderRadius) {
    Object.entries(config.components.borderRadius).forEach(([key, value]) => {
      if (key !== 'defaultRadius') {
        vars.push(`--radius-${key}: ${value}`)
      }
    })
  }

  // === 阴影 ===
  if (config.components.shadows) {
    Object.entries(config.components.shadows).forEach(([level, shadows]) => {
      if (level !== 'defaultShadow' && typeof shadows === 'object') {
        Object.entries(shadows).forEach(([size, value]) => {
          vars.push(`--shadow-${level}-${size}: ${value}`)
        })
      }
    })
  }

  // === shadcn/ui 桥接映射 ===
  // 将自定义主题变量映射到 shadcn/ui 组件期望的 HSL 原始格式变量
  // shadcn/ui 通过 hsl(var(--xxx)) 消费这些变量，所以这里存储的是不带 hsl() 包裹的原始值
  const c = config.colors
  vars.push(`--foreground: ${colorToHsl(c.text.primary)}`)
  vars.push(`--background: ${colorToHsl(c.background.main)}`)
  vars.push(`--card: ${colorToHsl(c.background.card)}`)
  vars.push(`--card-foreground: ${colorToHsl(c.text.primary)}`)
  vars.push(`--popover: ${colorToHsl(c.background.elevated)}`)
  vars.push(`--popover-foreground: ${colorToHsl(c.text.primary)}`)
  vars.push(`--panel-solid: ${colorToHslSolid(c.background.elevated)}`)
  vars.push(`--primary: ${colorToHsl(c.primary)}`)
  vars.push(`--primary-foreground: ${colorToHsl(c.bubble.user_text)}`)
  vars.push(`--secondary: ${colorToHsl(c.secondary)}`)
  vars.push(`--secondary-foreground: ${colorToHsl(c.text.primary)}`)
  vars.push(`--muted: ${colorToHsl(c.background.input)}`)
  vars.push(`--muted-foreground: ${colorToHsl(c.text.secondary)}`)
  vars.push(`--accent: ${colorToHsl(c.accent)}`)
  vars.push(`--accent-foreground: ${colorToHsl(c.text.primary)}`)
  vars.push(`--border: ${colorToHsl(c.border.default)}`)
  vars.push(`--input: ${colorToHsl(c.background.input)}`)
  vars.push(`--ring: ${colorToHsl(c.primary)}`)

  // === 视觉效果（effects）→ 全局过渡/动画语义变量 ===
  // effects.transitionDuration 是主题级过渡时长基准；effects.animations=false 时
  // 全站过渡归零（无障碍主题如 high-contrast 据此关闭动画）。
  // 组件统一引用 var(--transition-*) / var(--transition-easing)，由 effects 单点驱动。
  const fx = config.effects
  const duration = fx?.transitionDuration ?? 200
  const easing = fx?.transitionEasing ?? 'cubic-bezier(0.4, 0, 0.2, 1)'
  const motion = fx?.animations ?? true
  vars.push(`--transition-easing: ${easing}`)
  // animations=false → 时长归零；否则按 fast/base/slow = 0.6x / 1x / 1.5x 派生三档
  const d = motion ? duration : 0
  vars.push(`--transition-fast: ${Math.round(d * 0.6)}ms ${easing}`)
  vars.push(`--transition-base: ${d}ms ${easing}`)
  vars.push(`--transition-slow: ${Math.round(d * 1.5)}ms ${easing}`)

  return vars.join('; ')
}

/**
 * 应用主题到 DOM
 *
 * 将主题配置批量应用到 document.documentElement
 *
 * @param config - 主题配置
 * @param debug - 是否输出调试信息
 */
export function applyTheme(config: ThemeConfig, debug = false): void {
  if (debug) {
    console.group('🎨 应用主题')
    console.log('主题 ID:', config.id)
    console.log('主题名称:', config.name)
  }

  const root = document.documentElement

  // 设置主题类名（用于 Tailwind 的 dark 模式）
  root.classList.remove('light', 'dark')
  if (config.category === 'dark') {
    root.classList.add('dark')
  } else if (config.category === 'light') {
    root.classList.add('light')
  }

  // 编译并应用 CSS 变量 - 使用 setProperty 而不是覆盖 cssText
  const cssVars = compileThemeVariables(config)
  const varEntries = cssVars.split(';').filter((v) => v.trim())

  varEntries.forEach((entry) => {
    const [key, value] = entry.split(':').map((s) => s.trim())
    if (key && value) {
      root.style.setProperty(key, value)
    }
  })

  // 应用背景样式
  if (config.backgrounds?.main) {
    if (config.backgrounds.main.type === 'gradient') {
      root.style.setProperty('--bg-main-gradient', config.backgrounds.main.value)
      document.body.style.background = config.backgrounds.main.value
    } else {
      root.style.setProperty('--bg-main-gradient', 'none')
      document.body.style.background = config.backgrounds.main.value
    }
  }

  if (debug) {
    console.log(`✅ 应用了 ${varEntries.length} 个 CSS 变量`)
    console.groupEnd()
  }
}

/**
 * 清除主题样式
 *
 * 移除所有主题相关的 CSS 变量和类名
 */
export function clearTheme(): void {
  const root = document.documentElement
  root.classList.remove('light', 'dark')
  root.style.cssText = ''
}

/**
 * 主题合并工具
 *
 * 合并基础主题和用户自定义配置
 *
 * @param base - 基础主题配置
 * @param custom - 用户自定义配置
 * @returns 合并后的主题配置
 */
export function mergeTheme(base: ThemeConfig, custom: Partial<ThemeConfig>): ThemeConfig {
  return mergeUserTheme(base, custom)
}

/**
 * 动态主题清单条目（后端 /api/v1/themes/manifest 返回的单项）
 */
interface ThemeManifestItem {
  id: string
  name: string
  url: string
}

/**
 * 拉取并加载动态主题（自动发现，无需用户点导入）
 *
 * 流程：
 * 1. GET /api/v1/themes/manifest 拿清单（后端无状态扫描 public/themes/*.json）
 * 2. 对每个条目 fetch 其 JSON 内容
 * 3. 调现成的 ThemeStorageService.importTheme 存入 localStorage
 *
 * 失败容错：清单拉取失败或单个主题 fetch/导入失败，只 console.warn 不抛出，
 * 保证后端不可达时前端降级到内置 preset（符合「失败兜底不影响整体」原则）。
 *
 * 幂等：importTheme 内部按 id 去重，重复加载只更新不新增。
 */
export async function fetchDynamicThemes(): Promise<void> {
  let manifest: ThemeManifestItem[]
  try {
    const { data } = await apiClient.get<ThemeManifestItem[]>(API_ENDPOINTS.THEMES.MANIFEST)
    manifest = data
  } catch (err) {
    // 后端不可达：静默降级，不影响现有 preset + localStorage 主题
    console.warn('[themeService] 动态主题清单拉取失败，降级到内置主题', err)
    return
  }

  if (!Array.isArray(manifest) || manifest.length === 0) {
    return
  }

  await Promise.all(
    manifest.map(async (item) => {
      try {
        const resp = await fetch(item.url)
        if (!resp.ok) {
          console.warn(`[themeService] 主题 ${item.id} 加载失败: HTTP ${resp.status}`)
          return
        }
        const configJson = await resp.text()
        // 复用 importTheme 的校验 + 存储逻辑（按 id 去重）
        ThemeStorageService.importTheme(configJson)
      } catch (err) {
        console.warn(`[themeService] 主题 ${item.id} 导入失败，跳过`, err)
      }
    }),
  )
}

/**
 * 验证主题配置
 *
 * 检查主题配置是否完整有效
 *
 * @param config - 主题配置
 * @returns 验证结果
 */
export function validateThemeConfig(config: unknown): { valid: boolean; errors?: string[] } {
  const errors: string[] = []

  if (!config || typeof config !== 'object') {
    return { valid: false, errors: ['配置不是对象'] }
  }

  const theme = config as Partial<ThemeConfig>

  // 检查必需字段
  if (!theme.id || typeof theme.id !== 'string') {
    errors.push('缺少或无效的 id 字段')
  }

  if (!theme.name || typeof theme.name !== 'string') {
    errors.push('缺少或无效的 name 字段')
  }

  if (!theme.colors || typeof theme.colors !== 'object') {
    errors.push('缺少或无效的 colors 字段')
  } else {
    // 检查必需的颜色字段
    const requiredColorFields = ['primary', 'secondary', 'accent', 'background', 'text', 'border']
    for (const field of requiredColorFields) {
      if (!(field in theme.colors)) {
        errors.push(`缺少必需的颜色字段: ${field}`)
      }
    }
  }

  if (!theme.components || typeof theme.components !== 'object') {
    errors.push('缺少或无效的 components 字段')
  }

  if (!theme.effects || typeof theme.effects !== 'object') {
    errors.push('缺少或无效的 effects 字段')
  }

  if (!theme.backgrounds || typeof theme.backgrounds !== 'object') {
    errors.push('缺少或无效的 backgrounds 字段')
  }

  return {
    valid: errors.length === 0,
    errors: errors.length > 0 ? errors : undefined,
  }
}

/**
 * 转换驼峰命名为短横线命名
 *
 * @param str - 驼峰命名字符串
 * @returns 短横线命名字符串
 */
function kebabCase(str: string): string {
  return str.replace(/([a-z])([A-Z])/g, '$1-$2').toLowerCase()
}

/**
 * 将 HEX 颜色值转换为 RGB 对象
 *
 * @param hex - HEX 颜色值（如 #3b82f6 或 #fff）
 * @returns RGB 对象，如果解析失败则返回 null
 */
function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const match = hex.replace(/^#/, '').match(/^([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i)
  if (!match) return null
  return {
    r: parseInt(match[1], 16),
    g: parseInt(match[2], 16),
    b: parseInt(match[3], 16),
  }
}

/**
 * 将 RGB 值转换为 HSL 格式字符串
 *
 * 输出格式为 shadcn/ui 期望的原始 HSL 值（不含 hsl() 包裹），
 * 如 "210 40% 98%" 或 "210 40% 98% / 0.5"（带透明度）
 *
 * @param r - 红色通道 (0-255)
 * @param g - 绿色通道 (0-255)
 * @param b - 蓝色通道 (0-255)
 * @param alpha - 可选透明度 (0-1)
 * @returns HSL 格式字符串
 */
function rgbToHsl(r: number, g: number, b: number, alpha?: number): string {
  const rn = r / 255
  const gn = g / 255
  const bn = b / 255
  const max = Math.max(rn, gn, bn)
  const min = Math.min(rn, gn, bn)
  const l = (max + min) / 2
  let h = 0
  let s = 0

  if (max !== min) {
    const d = max - min
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min)
    switch (max) {
      case rn:
        h = ((gn - bn) / d + (gn < bn ? 6 : 0)) / 6
        break
      case gn:
        h = ((bn - rn) / d + 2) / 6
        break
      case bn:
        h = ((rn - gn) / d + 4) / 6
        break
    }
  }

  const hDeg = Math.round(h * 360)
  const sPct = Math.round(s * 100)
  const lPct = Math.round(l * 100)

  if (alpha !== undefined && alpha < 1) {
    return `${hDeg} ${sPct}% ${lPct}% / ${alpha}`
  }
  return `${hDeg} ${sPct}% ${lPct}%`
}

/**
 * 将任意颜色值转换为 HSL 原始格式
 *
 * 支持 HEX (#rrggbb) 和 RGBA (rgba(r,g,b,a)) 格式，
 * 输出 shadcn/ui 期望的 HSL 原始值（用于 hsl(var(--xxx)) 模式）
 *
 * @param color - 颜色值字符串
 * @returns HSL 格式字符串，解析失败时返回原值
 */
function colorToHsl(color: string): string {
  if (color.startsWith('#')) {
    const rgb = hexToRgb(color)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  const rgbaMatch = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgbaMatch) {
    const r = parseInt(rgbaMatch[1])
    const g = parseInt(rgbaMatch[2])
    const b = parseInt(rgbaMatch[3])
    const a = rgbaMatch[4] !== undefined ? parseFloat(rgbaMatch[4]) : undefined
    return rgbToHsl(r, g, b, a)
  }

  return color
}

/**
 * 将颜色转换为不透明的 HSL 原始格式
 *
 * 与 colorToHsl 相同，但强制忽略 alpha 通道，确保输出为完全不透明
 *
 * @param color - 颜色值字符串
 * @returns 不透明的 HSL 格式字符串
 */
function colorToHslSolid(color: string): string {
  if (color.startsWith('#')) {
    const rgb = hexToRgb(color)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  const rgbaMatch = color.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)/)
  if (rgbaMatch) {
    const r = parseInt(rgbaMatch[1])
    const g = parseInt(rgbaMatch[2])
    const b = parseInt(rgbaMatch[3])
    return rgbToHsl(r, g, b)
  }

  return color
}
