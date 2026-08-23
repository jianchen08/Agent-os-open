/**
 * 主题服务
 *
 * 提供主题应用、合并等工具函数
 * 主题配置由前端管理：预设主题打包进 bundle，动态主题通过 Vite 静态导入
 * （import.meta.glob）发现，纯前端资源，不依赖后端。
 */

import { presetThemes } from '@/config/themes'
import { ThemeStorageService, mergeTheme as mergeUserTheme } from '@/services/themeStorage'
import type { PluginTheme, ThemeConfig, ThemeInfo } from '@/types/theme'

/**
 * 获取预设主题配置
 *
 * 注册点收敛：预设主题统一在 @/config/themes 的 presetThemes 维护，
 * 此处仅做查找，新增预设主题不再需要改本文件。
 *
 * @param themeId - 主题 ID
 * @returns 主题配置，如果不存在则返回 null
 */
export function getPresetTheme(themeId: string): ThemeConfig | null {
  return presetThemes[themeId] || null
}

/**
 * 获取所有预设主题
 *
 * @returns 预设主题列表
 */
export function getAllPresetThemes(): ThemeConfig[] {
  return Object.values(presetThemes)
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

  // === 全局边框线型 ===
  // Tailwind preflight 将 border-style 写死 solid，颜色类（border-border 等）无法
  // 控制线型；输出 --border-line-style 并在 index.css 全局覆盖，
  // 使全站边框跟随主题（像素=dashed 虚线描边 / 软萌=dotted 圆点线）。
  vars.push(`--border-line-style: ${config.components.borderStyle || 'solid'}`)

  // === 悬停叠加层 ===
  // 替代散布在侧边栏/面板的硬编码 bg-white/5（浅色主题下白上叠白不可见）：
  // 深色主题叠白、浅色主题叠深，保证任何主题下 hover 反馈可见。
  const isDarkTheme = config.category === 'dark'
  const hoverOverlay = isDarkTheme ? 'rgba(255, 255, 255, 0.06)' : 'rgba(15, 23, 42, 0.06)'
  vars.push(`--hover-overlay: ${hoverOverlay}`)

  // === 遮罩层 ===
  // 由主题 dialog.overlay 配置派生（overlayBg + overlayOpacity → rgba），
  // 替代硬编码 bg-black/50；解析失败时回退深色半透明。
  const dialogCfg = config.components.dialog
  const overlayRgb = dialogCfg?.overlayBg ? hexToRgb(dialogCfg.overlayBg) : null
  const overlayOpacity = dialogCfg?.overlayOpacity ?? 0.5
  vars.push(
    `--overlay-bg: ${overlayRgb ? `rgba(${overlayRgb.r}, ${overlayRgb.g}, ${overlayRgb.b}, ${overlayOpacity})` : 'rgba(0, 0, 0, 0.5)'}`,
  )
  // 媒体查看器（lightbox）底幕：恒定深色以保证图片对比度，
  // 仅深浅略调（浅色主题带一点蓝调而非死黑）
  vars.push(`--overlay-strong: ${isDarkTheme ? 'rgba(0, 0, 0, 0.8)' : 'rgba(15, 23, 42, 0.78)'}`)

  // === Deep Space v2 桥接变量（--ds-*） ===
  // 组件大量引用 deep-space-v2.css 的 --ds-* 变量（该文件按 .dark/.light class 定死取值，
  // 不覆写则非深空主题下这些区域不跟随主题）。
  // 这里以主题内联样式（优先级高于 class 规则）统一覆写整套 --ds-*，
  // 使全部既有引用零改动接入主题系统；引擎未运行时仍回落 CSS 静态定义。
  const c2 = config.colors
  const map: Record<string, string> = {
    // 文字
    '--ds-text-primary': c2.text.primary,
    '--ds-text-secondary': c2.text.secondary,
    '--ds-text-muted': c2.text.muted,
    '--ds-text-disabled': c2.text.disabled,
    // 背景（canvas 允许渐变值，均用于 background 位）
    '--ds-bg-canvas': c2.background.main,
    '--ds-bg-panel': c2.background.card,
    '--ds-bg-elevated': c2.background.elevated,
    '--ds-bg-hover': hoverOverlay,
    // 边框
    '--ds-border-subtle': c2.border.default,
    '--ds-border-active': c2.border.active,
    '--ds-border-strong': c2.border.hover,
    // 强调色
    '--ds-accent-primary': c2.primary,
    '--ds-accent-ai': c2.accent,
    '--ds-accent-blue': c2.secondary,
    '--ds-accent-glow': config.components.glow?.running || c2.primary,
    // 状态色（status 无 waiting，映射到 pending）
    '--ds-status-success': c2.status.success,
    '--ds-status-error': c2.status.error,
    '--ds-status-info': c2.status.info,
    '--ds-status-running': c2.status.running,
    '--ds-status-pending': c2.status.pending,
    '--ds-status-waiting': c2.status.pending,
  }
  Object.entries(map).forEach(([key, value]) => {
    if (value) vars.push(`${key}: ${value}`)
  })

  // 圆角（ds-r-*，组件目前未引用，输出以保持桥接完整）
  const br = config.components.borderRadius
  if (br) {
    vars.push(`--ds-r-sm: ${br.sm}`)
    vars.push(`--ds-r-md: ${br.md}`)
    vars.push(`--ds-r-lg: ${br.lg}`)
    vars.push(`--ds-r-xl: ${br.xl}`)
    vars.push(`--ds-r-bubble: ${br.xl}`)
  }

  // === 动画/辉光语义色 ===
  // theme.css 静态定义了 --accent-*（引擎未跑时兜底），tailwind 的
  // border-flow 动画与 glow-running/waiting 工具类引用它们 —— 此处按主题覆写，
  // 并补齐工具类实际引用的 --shadow-glow-*。
  vars.push(`--accent-running: ${config.colors.status.running}`)
  vars.push(`--accent-waiting: ${config.colors.status.pending}`)
  vars.push(`--accent-success: ${config.colors.status.success}`)
  vars.push(`--accent-error: ${config.colors.status.error}`)
  if (config.components.glow) {
    vars.push(`--shadow-glow-running: ${config.components.glow.running}`)
    vars.push(`--shadow-glow-waiting: ${config.components.glow.waiting}`)
  }

  // === 代码块底色 ===
  // 深色主题固定深底（兼容语法高亮配色）；浅色主题用主题 input 色（淡主题色调）
  vars.push(`--code-bg: ${isDarkTheme ? 'rgba(15, 23, 42, 0.85)' : config.colors.background.input}`)

  // === 字体族 ===
  // 主题字体接入全局：body 与 Tailwind font-ui/font-code 均消费这两个变量，
  // 未输出时消费端回退 design-tokens.css 的 --font-family / --font-family-mono。
  if (config.components.fonts?.ui) {
    vars.push(`--font-ui: ${config.components.fonts.ui}`)
  }
  if (config.components.fonts?.code) {
    vars.push(`--font-code: ${config.components.fonts.code}`)
  }

  // === 字号阶梯 ===
  // fontSize 输出两套变量（Tailwind 工具类引用与语义阶梯）：
  // 1) --text-xs~xl:接管 Tailwind 默认字号工具类，存量页面的 text-xs/sm/base
  //    零改动即跟随主题；行高按 1.5 倍率同步输出，避免字号变大后行距局促。
  // 2) --font-size-caption~page-title:语义阶梯（tailwind.config.js 的
  //    text-caption/label/body/title/page-title 消费）。
  // 引擎未运行时无内联覆写，回落 Tailwind / design-tokens 静态默认，观感不变。
  const fs = config.components.fontSize
  if (fs) {
    const scale: Array<[string, string]> = [
      ['xs', fs.xs],
      ['sm', fs.sm],
      ['base', fs.md],
      ['lg', fs.lg],
      ['xl', fs.xl],
    ]
    scale.forEach(([step, size]) => {
      vars.push(`--text-${step}: ${size}`)
      vars.push(`--text-${step}--line-height: 1.5`)
    })
    vars.push(`--font-size-caption: ${fs.xs}`)
    vars.push(`--font-size-label: ${fs.sm}`)
    vars.push(`--font-size-body: ${fs.md}`)
    vars.push(`--font-size-title: ${fs.lg}`)
    vars.push(`--font-size-page-title: ${fs.xl}`)
  }

  // === 区域背景（侧边栏 / 聊天区） ===
  // backgrounds.sidebar/chat 是主题的区域背景配置；缺省时回退 colors.background
  // 对应色，保证浅色/深色基础主题下变量始终有值。
  const sidebarAreaBg =
    config.backgrounds?.sidebar?.value || config.colors.background.sidebar
  if (sidebarAreaBg) {
    vars.push(`--sidebar-bg: ${sidebarAreaBg}`)
  }
  const chatAreaBg = config.backgrounds?.chat?.value || config.colors.background.main
  if (chatAreaBg) {
    vars.push(`--chat-bg: ${chatAreaBg}`)
    // 聊天区背景双通道：渐变只能进 background-image 位（background-color 位塞渐变
    // 会整条失效→聊天区透明→body 全屏纹理层穿透内容区）。
    // .theme-chat-area 按 image/color 两位分别消费，纯色与渐变主题统一承载。
    const isGradientBg = /gradient\(/.test(chatAreaBg)
    vars.push(`--chat-bg-image: ${isGradientBg ? chatAreaBg : 'none'}`)
    vars.push(`--chat-bg-color: ${isGradientBg ? 'transparent' : chatAreaBg}`)
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
 * 应用插件主题的变量与背景覆盖（contributes.themes）
 *
 * 必须在基础主题已应用（applyTheme）之后调用：声明的变量逐个 setProperty
 * （后写者胜，覆盖 base 主题对应值）；背景（image/texture）按 enabled 开关
 * 覆盖宿主背景。纯数据无 JS 执行，零 eval 风险（任务文档「第 1 层」）。
 *
 * 变量生命周期统一管理（2026-08-21 修：切主题漂移根因）：插件发射的变量
 * 记录在 lastPluginVars，下次应用前先清理——皮肤切回内置主题时 base 主题
 * 不发射 --region-* 等插件专属变量，不清理则区域背景/气泡形态继续命中
 * 旧皮肤值（侧边栏停留在上一主题配置）。
 *
 * @param theme - 插件主题声明（plugin.json contributes.themes 条目）
 */
let lastPluginVars: string[] = []

/** 清理上一插件主题发射的变量（applyTheme 每次应用前调用，统一生命周期） */
export function clearPluginThemeVars(): void {
  const root = document.documentElement
  lastPluginVars.forEach((key) => root.style.removeProperty(key))
  lastPluginVars = []
}

export function applyPluginThemeVars(theme: PluginTheme): void {
  const root = document.documentElement
  const body = document.body

  // === 变量覆盖 ===
  clearPluginThemeVars()
  const variables = theme.variables ?? {}
  for (const [key, value] of Object.entries(variables)) {
    if (key && value) {
      root.style.setProperty(key, value)
      lastPluginVars.push(key)
    }
  }

  // === 背景覆盖 ===
  const bg = theme.backgrounds
  if (bg?.image) {
    // enabled=false 显式关闭宿主背景图片；未声明 enabled 视为按配置覆盖
    if (bg.image.enabled === false) {
      body.classList.remove('has-bg-image')
      root.style.removeProperty('--bg-image')
    } else if (bg.image.url) {
      body.classList.add('has-bg-image')
      if (bg.image.position) root.style.setProperty('--bg-image-position', bg.image.position)
      if (bg.image.size) root.style.setProperty('--bg-image-size', bg.image.size)
      if (bg.image.attachment) root.style.setProperty('--bg-image-attachment', bg.image.attachment)
      if (bg.image.overlay) root.style.setProperty('--bg-overlay', bg.image.overlay)
      if (bg.image.overlayOpacity !== undefined) {
        root.style.setProperty('--bg-overlay-opacity', String(bg.image.overlayOpacity))
      }
      root.style.setProperty('--bg-image', `url(${bg.image.url})`)
    }
  }
  if (bg?.texture) {
    if (bg.texture.enabled === false || bg.texture.type === 'none') {
      root.style.setProperty('--bg-texture', 'none')
    } else if (bg.texture.type) {
      root.style.setProperty('--bg-texture', generateTextureCSS(bg.texture))
      if (bg.texture.size) root.style.setProperty('--bg-texture-size', bg.texture.size)
      if (bg.texture.opacity !== undefined) {
        root.style.setProperty('--bg-texture-opacity', String(bg.texture.opacity))
      }
    }
  }
}

/**
 * 为插件主题派生预览色（ThemePanel/主题卡片色块用）
 *
 * 优先取插件声明的 --ds-* 变量（accent-primary/bg-canvas/bg-panel/text-primary/
 * accent-ai），缺省回退其 base 主题（dark/light 预设）的预览色。
 *
 * @param theme - 插件主题声明
 * @returns 预览色对象
 */
export function derivePluginThemePreview(theme: PluginTheme): ThemeInfo['preview'] {
  const vars = theme.variables ?? {}
  const basePreview = presetThemes[theme.base]?.preview
  return {
    primary: vars['--ds-accent-primary'] ?? basePreview?.primary ?? '#22D3EE',
    background: vars['--ds-bg-canvas'] ?? basePreview?.background ?? '#04060F',
    surface: vars['--ds-bg-panel'] ?? basePreview?.surface ?? '#0A1226',
    text: vars['--ds-text-primary'] ?? basePreview?.text ?? '#F1F5F9',
    accent: vars['--ds-accent-ai'] ?? basePreview?.accent ?? '#A78BFA',
  }
}

/**
 * 生成纹理 CSS（插件主题背景用；与 themeStore.generateTextureCSS 语义一致）
 */
function generateTextureCSS(texture: { type?: string; color?: string; size?: string }): string {
  if (!texture?.type || texture.type === 'none') return 'none'
  const { type, color = 'rgba(255,255,255,0.03)', size = '24px' } = texture
  switch (type) {
    case 'dots':
      return `radial-gradient(${color} 1px, transparent 1px)`
    case 'grid':
      return `linear-gradient(${color} 1px, transparent 1px), linear-gradient(90deg, ${color} 1px, transparent 1px)`
    case 'lines':
      return `repeating-linear-gradient(0deg, ${color}, ${color} 1px, transparent 1px, transparent ${size})`
    case 'checker':
      return `repeating-conic-gradient(${color} 0% 25%, transparent 0% 50%)`
    default:
      return 'none'
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
 * 拉取并加载动态主题（自动发现，无需用户点导入）
 *
 * 主题是纯前端资源（public/themes/*.json），无业务逻辑、无鉴权、无多用户，
 * 不需要内核参与。改用 Vite 静态导入（import.meta.glob）在构建期发现所有主题，
 * 替代原先「GET manifest → fetch 每个 url」的两步流程（已删内核 themes 端点）。
 *
 * 失败容错：单个主题导入失败只 console.warn 不抛出。
 * 幂等：importTheme 内部按 id 去重，重复加载只更新不新增。
 */
export async function fetchDynamicThemes(): Promise<void> {
  // Vite 构建期扫描 public/themes/*.json（eager: 直接拿到 JSON 对象）
  const modules = import.meta.glob('/themes/*.json', { eager: true, import: 'default' })
  const entries = Object.entries(modules)
  if (entries.length === 0) return

  await Promise.all(
    entries.map(async ([path, config]) => {
      try {
        // config 已是解析后的 JSON 对象，importTheme 接受 JSON 字符串
        const configJson = JSON.stringify(config)
        ThemeStorageService.importTheme(configJson)
      } catch (err) {
        console.warn(`[themeService] 主题 ${path} 导入失败，跳过`, err)
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
 * 从渐变等复杂颜色值中提取实色（取色标中位近似整体观感）
 *
 * 渐变字符串塞进 hsl(var(--xxx)) 桥接会全线失效（面板透明），
 * 这里为 shadcn 桥接提取一个可解析的实色近似值。
 *
 * @param color - 颜色值字符串
 * @returns 实色 HEX，无法提取时返回 null
 */
function extractSolidFromGradient(color: string): string | null {
  const stops = color.match(/#[0-9a-f]{6}\b/gi)
  if (!stops || stops.length === 0) return null
  return stops[Math.floor((stops.length - 1) / 2)]
}

/**
 * 将任意颜色值转换为 HSL 原始格式
 *
 * 支持 HEX (#rrggbb) 和 RGBA (rgba(r,g,b,a)) 格式，
 * 输出 shadcn/ui 期望的 HSL 原始值（用于 hsl(var(--xxx)) 模式）。
 * 渐变值提取色标中位转实色（渐变原样输出会让 hsl() 桥接全线失效）。
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

  const solidFromGradient = extractSolidFromGradient(color)
  if (solidFromGradient) {
    const rgb = hexToRgb(solidFromGradient)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  return color
}

/**
 * 将颜色转换为不透明的 HSL 原始格式
 *
 * 与 colorToHsl 相同，但强制忽略 alpha 通道，确保输出为完全不透明；
 * 渐变值同样提取色标中位转实色。
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

  const solidFromGradient = extractSolidFromGradient(color)
  if (solidFromGradient) {
    const rgb = hexToRgb(solidFromGradient)
    if (rgb) return rgbToHsl(rgb.r, rgb.g, rgb.b)
  }

  return color
}
