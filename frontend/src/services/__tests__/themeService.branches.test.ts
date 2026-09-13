// @feature: FP-T12 补测 | @ci: frontend-test
// @feature: 主题系统 | @ci: frontend-test
/**
 * themeService 分支缺口补测（branches 冲刺批九）：
 * 编译器各变量域的缺省/显式双面（button 变体、badge/toast 变体子集、输入框/卡片、
 * 字体/字号、区域背景回退、effects 三档回退、颜色解析容错通道）、applyTheme 的
 * 非亮暗类目 / 空值变量跳过 / 无主背景、applyPluginThemeVars 的最小声明形态。
 *
 * 行为契约：公开 API 输入 → 编译出的 CSS 变量字符串 / DOM 副作用断言。
 */

import { describe, it, expect, beforeEach, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import {
  applyPluginThemeVars,
  applyTheme,
  compileThemeVariables,
  getPresetTheme,
} from '@/services/themeService'
import type { PluginTheme, ThemeConfig } from '@/types/theme'

const { importTheme } = vi.hoisted(() => ({ importTheme: vi.fn() }))
vi.mock('@/services/themeStorage', () => ({
  ThemeStorageService: { importTheme },
}))

const dark = presetThemes['dark']

/** 浅拷贝 dark 并按域打补丁（cast 放宽：编译器对各域的缺省键本就逐项判空） */
function theme(patch: {
  colors?: Partial<ThemeConfig['colors']>
  components?: Record<string, unknown>
  effects?: Record<string, unknown>
  backgrounds?: Record<string, unknown> | null
  category?: ThemeConfig['category']
} = {}): ThemeConfig {
  return {
    ...dark,
    ...(patch.category !== undefined ? { category: patch.category } : {}),
    colors: { ...dark.colors, ...patch.colors },
    components: {
      ...(patch.components ?? {}),
    } as ThemeConfig['components'],
    effects: {
      ...(patch.effects ?? {}),
    } as ThemeConfig['effects'],
    backgrounds: (patch.backgrounds === null
      ? undefined
      : { ...dark.backgrounds, ...patch.backgrounds }) as ThemeConfig['backgrounds'],
  }
}

function pluginTheme(overrides: Partial<PluginTheme> = {}): PluginTheme {
  return {
    id: 'gap-skin',
    name: '缺口皮肤',
    base: 'dark',
    pluginId: 'demo_plugin',
    ...overrides,
  }
}

describe('getPresetTheme — 查找契约（双输入组）', () => {
  it.each([
    ['dark', 'dark'],
    ['light', 'light'],
  ])('已注册 id %s 命中预设', (id, expected) => {
    expect(getPresetTheme(id)?.id).toBe(expected)
  })

  it('未注册 id 返回 null（不抛错）', () => {
    expect(getPresetTheme('definitely-not-registered')).toBeNull()
  })
})

describe('compileThemeVariables — 选中态派生的不可解析主色容错', () => {
  it('primary 无法解析为 RGB：不发射 --selection-*（避免 NaN 变量），--primary 仍透传', () => {
    const vars = compileThemeVariables(theme({ colors: { primary: 'not-a-color' } }))
    expect(vars).not.toContain('--selection-bg:')
    expect(vars).not.toContain('--selection-text:')
    expect(vars).toContain('--primary: not-a-color')
  })

  it('primary 为合法 hex：深浅族决定选中底透明度与前景', () => {
    const darkVars = compileThemeVariables(theme())
    expect(darkVars).toContain('--selection-text: #ffffff')
    expect(darkVars).toMatch(/--selection-bg: rgba\(34, 211, 238, 0\.35\)/)
  })
})

describe('compileThemeVariables — 状态色解析容错通道', () => {
  it('状态色为空串：不发射 -rgb 三元组，前景黑白择优回退白（无法解析按深底）', () => {
    const vars = compileThemeVariables(
      theme({ colors: { status: { ...dark.colors.status, pending: '' } } }),
    )
    expect(vars).not.toContain('--status-pending-rgb:')
    expect(vars).toContain('--status-pending-foreground: #ffffff')
  })

  it('状态色为 rgba()：解析出 rgb 三元组供 tailwind 透明度修饰消费', () => {
    const vars = compileThemeVariables(
      theme({ colors: { status: { ...dark.colors.status, running: 'rgba(10, 200, 80, 0.9)' } } }),
    )
    expect(vars).toContain('--status-running-rgb: 10 200 80')
  })

  it('气泡面为 rgb() 直色：链接色经 colorToRgb 直取通道发射（122 直取面）', () => {
    const vars = compileThemeVariables(
      theme({ colors: { bubble: { ...dark.colors.bubble, user_bg: 'rgb(4, 6, 15)' } } }),
    )
    expect(vars).toContain('--bubble-link:')
  })
})

describe('compileThemeVariables — 气泡可选形态字段的缺省面', () => {
  it('气泡仅声明必备四色：radius/shadow/border/padding 全部不发射', () => {
    const vars = compileThemeVariables(
      theme({
        colors: {
          bubble: {
            user_bg: '#112233',
            user_text: '#ffffff',
            ai_bg: '#445566',
            ai_text: '#eeeeee',
          } as ThemeConfig['colors']['bubble'],
        },
      }),
    )
    for (const v of [
      '--bubble-user-radius:',
      '--bubble-user-shadow:',
      '--bubble-user-border:',
      '--bubble-user-padding:',
      '--bubble-ai-radius:',
      '--bubble-ai-shadow:',
      '--bubble-ai-border:',
      '--bubble-ai-padding:',
    ]) {
      expect(vars).not.toContain(v)
    }
    // 必备四色与链接色仍在
    expect(vars).toContain('--bubble-user-bg: #112233')
    expect(vars).toContain('--bubble-link:')
  })

  it('可选字段逐项声明 → 逐项发射（声明什么发什么）', () => {
    const vars = compileThemeVariables(
      theme({
        colors: {
          bubble: {
            ...dark.colors.bubble,
            user_border: '1px solid #fff',
            user_padding: '10px',
            ai_border: '1px solid #000',
            ai_padding: '8px',
          },
        },
      }),
    )
    expect(vars).toContain('--bubble-user-radius: 1rem 1rem 1rem 0.25rem')
    expect(vars).toContain('--bubble-user-shadow: 0 2px 8px rgba(34, 211, 238, 0.2)')
    expect(vars).toContain('--bubble-user-border: 1px solid #fff')
    expect(vars).toContain('--bubble-user-padding: 10px')
    expect(vars).toContain('--bubble-ai-border: 1px solid #000')
    expect(vars).toContain('--bubble-ai-padding: 8px')
  })
})

describe('compileThemeVariables — button 组件矩阵', () => {
  it('无 variants：仅发形态变量（radius/shadow/effect），四个变体变量全部缺席', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          button: { style: 'pill', shadow: false, hoverEffect: 'lift' },
        },
      }),
    )
    expect(vars).toContain('--btn-radius: 9999px')
    expect(vars).toContain('--btn-shadow: none')
    expect(vars).toContain('--btn-shadow-hover: none')
    expect(vars).toContain('--btn-hover-effect: lift')
    for (const v of [
      '--btn-primary-bg:',
      '--btn-secondary-bg:',
      '--btn-ghost-bg:',
      '--btn-destructive-bg:',
      '--btn-primary-hover-bg:',
    ]) {
      expect(vars).not.toContain(v)
    }
  })

  it('四变体齐全但无 hoverBg：变体底/字/边发射，hover 底缺席', () => {
    const variants = {
      primary: { bg: '#a', text: '#b', border: '#c' },
      secondary: { bg: '#d', text: '#e', border: '#f' },
      ghost: { bg: '#1', text: '#2', border: '#3' },
      destructive: { bg: '#4', text: '#5', border: '#6' },
    }
    const vars = compileThemeVariables(
      theme({ components: { button: { style: 'rounded', shadow: true, hoverEffect: 'none', variants } } }),
    )
    expect(vars).toContain('--btn-primary-bg: #a')
    expect(vars).toContain('--btn-secondary-border: #f')
    expect(vars).toContain('--btn-ghost-text: #2')
    expect(vars).toContain('--btn-destructive-bg: #4')
    expect(vars).toContain('--btn-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1)')
    expect(vars).not.toContain('--btn-primary-hover-bg:')
    expect(vars).not.toContain('--btn-secondary-hover-bg:')
    expect(vars).not.toContain('--btn-ghost-hover-bg:')
    expect(vars).not.toContain('--btn-destructive-hover-bg:')
  })

  it('hoverBg 声明即发射；style 未知名回退 0.5rem、square 走映射', () => {
    const variants = {
      primary: { bg: '#a', text: '#b', border: '#c', hoverBg: '#h1' },
      secondary: { bg: '#d', text: '#e', border: '#f', hoverBg: '#h2' },
      ghost: { bg: '#1', text: '#2', border: '#3', hoverBg: '#h3' },
      destructive: { bg: '#4', text: '#5', border: '#6', hoverBg: '#h4' },
    }
    const vars = compileThemeVariables(
      theme({ components: { button: { style: 'mystery-style', shadow: false, hoverEffect: 'x', variants } } }),
    )
    expect(vars).toContain('--btn-primary-hover-bg: #h1')
    expect(vars).toContain('--btn-secondary-hover-bg: #h2')
    expect(vars).toContain('--btn-ghost-hover-bg: #h3')
    expect(vars).toContain('--btn-destructive-hover-bg: #h4')
    expect(vars).toContain('--btn-radius: 0.5rem')

    const squareVars = compileThemeVariables(
      theme({ components: { button: { style: 'square', shadow: false, hoverEffect: 'x' } } }),
    )
    expect(squareVars).toContain('--btn-radius: 0.125rem')
  })

  it('button 整体缺席：不发射任何按钮变量', () => {
    const vars = compileThemeVariables(theme({ components: {} }))
    expect(vars).not.toContain('--btn-radius:')
    expect(vars).not.toContain('--btn-hover-effect:')
  })

  it('变体子集：缺哪个变体就不发哪组（167/177/187/197 缺省面）', () => {
    const variants = {
      secondary: { bg: '#d', text: '#e', border: '#f' },
      ghost: { bg: '#1', text: '#2', border: '#3' },
    }
    const vars = compileThemeVariables(
      theme({ components: { button: { style: 'rounded', shadow: false, hoverEffect: 'none', variants } } }),
    )
    expect(vars).toContain('--btn-secondary-bg: #d')
    expect(vars).toContain('--btn-ghost-bg: #1')
    expect(vars).not.toContain('--btn-primary-bg:')
    expect(vars).not.toContain('--btn-destructive-bg:')

    const onlyPrimary = compileThemeVariables(
      theme({
        components: {
          button: {
            style: 'rounded', shadow: false, hoverEffect: 'none',
            variants: { primary: { bg: '#a', text: '#b', border: '#c' } },
          },
        },
      }),
    )
    expect(onlyPrimary).toContain('--btn-primary-bg: #a')
    expect(onlyPrimary).not.toContain('--btn-secondary-bg:')
    expect(onlyPrimary).not.toContain('--btn-ghost-bg:')
    expect(onlyPrimary).not.toContain('--btn-destructive-bg:')
  })
})

describe('compileThemeVariables — input/card 缺省面', () => {
  it('input 空声明：focus 变量与 --input-style 不发射', () => {
    const vars = compileThemeVariables(theme({ components: { input: {} } }))
    expect(vars).not.toContain('--input-focus-border:')
    expect(vars).not.toContain('--input-focus-ring:')
    expect(vars).not.toContain('--input-style:')
  })

  it('input 全声明：focus 边框/光环/样式逐项发射', () => {
    const vars = compileThemeVariables(
      theme({ components: { input: { focusBorder: '#0af', focusGlow: '0 0 4px', style: 'outlined' } } }),
    )
    expect(vars).toContain('--input-focus-border: #0af')
    expect(vars).toContain('--input-focus-ring: 0 0 4px')
    expect(vars).toContain('--input-style: outlined')
  })

  it('background.input 为空串：组件域不发非空 --bg-input（244 缺省面），置值时组件域覆写生效', () => {
    const vars = compileThemeVariables(
      theme({ colors: { background: { ...dark.colors.background, input: '' } } }),
    )
    // 基础色域对空值仍发键（--bg-input: 空），组件域的 244 分支不再覆写
    expect(vars).not.toContain('--bg-input: #')
    const filled = compileThemeVariables(
      theme({ colors: { background: { ...dark.colors.background, input: '#244' } } }),
    )
    expect(filled).toContain('--bg-input: #244')
  })

  it('card 空声明：border/blur 不发射；声明后逐项发射', () => {
    const absent = compileThemeVariables(theme({ components: { card: {} } }))
    expect(absent).not.toContain('--card-border:')
    expect(absent).not.toContain('--card-backdrop-blur:')
    const full = compileThemeVariables(
      theme({ components: { card: { border: '1px solid #333', blur: '8px' } } }),
    )
    expect(full).toContain('--card-border: 1px solid #333')
    expect(full).toContain('--card-backdrop-blur: 8px')
  })
})

describe('compileThemeVariables — badge 变体子集', () => {
  const fullVariants = {
    default: { bg: '#a', text: '#b', border: '#c' },
    secondary: { bg: '#d', text: '#e', border: '#f' },
    success: { bg: '#g', text: '#h', border: '#i' },
    warning: { bg: '#j', text: '#k', border: '#l' },
    error: { bg: '#m', text: '#n', border: '#o' },
    info: { bg: '#p', text: '#q', border: '#r' },
  }

  it('badge 无 variants：仅发 radius，变体变量全缺席', () => {
    const vars = compileThemeVariables(
      theme({ components: { badge: { borderRadius: '4px' } } }),
    )
    expect(vars).toContain('--badge-radius: 4px')
    expect(vars).not.toContain('--badge-default-bg:')
    expect(vars).not.toContain('--badge-info-bg:')
  })

  it('badge 仅 default 变体：只发 default 三件套', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          badge: { borderRadius: '4px', variants: { default: fullVariants.default } },
        },
      }),
    )
    expect(vars).toContain('--badge-default-bg: #a')
    expect(vars).not.toContain('--badge-secondary-bg:')
    expect(vars).not.toContain('--badge-success-bg:')
    expect(vars).not.toContain('--badge-warning-bg:')
    expect(vars).not.toContain('--badge-error-bg:')
    expect(vars).not.toContain('--badge-info-bg:')
  })

  it('badge 全变体：六组 bg/text/border 齐发', () => {
    const vars = compileThemeVariables(
      theme({ components: { badge: { borderRadius: '2px', variants: fullVariants } } }),
    )
    expect(vars).toContain('--badge-success-border: #i')
    expect(vars).toContain('--badge-warning-text: #k')
    expect(vars).toContain('--badge-error-bg: #m')
    expect(vars).toContain('--badge-info-border: #r')
  })

  it('badge 整体缺席：不发 --badge-radius', () => {
    expect(compileThemeVariables(theme({ components: {} }))).not.toContain('--badge-radius:')
  })

  it('badge 变体缺 default：不发 default 组（263 缺省面）', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          badge: {
            borderRadius: '4px',
            variants: { secondary: { bg: '#d', text: '#e', border: '#f' } },
          },
        },
      }),
    )
    expect(vars).toContain('--badge-secondary-bg: #d')
    expect(vars).not.toContain('--badge-default-bg:')
  })
})

describe('compileThemeVariables — dialog/tabs/toast/progress/dropdown 缺省与子集', () => {
  it('五类组件整体缺席：对应变量全不发射，遮罩回退深色半透明', () => {
    const vars = compileThemeVariables(theme({ components: {} }))
    for (const v of [
      '--dialog-radius:',
      '--tabs-radius:',
      '--toast-radius:',
      '--progress-radius:',
      '--dropdown-radius:',
    ]) {
      expect(vars).not.toContain(v)
    }
    // dialog 缺席 → overlayBg 不可解析 → 回退 rgba(0,0,0,0.5)，且线型回退 solid
    expect(vars).toContain('--overlay-bg: rgba(0, 0, 0, 0.5)')
    expect(vars).toContain('--border-line-style: solid')
  })

  it('toast 仅 default/error 变体：success/warning/info 缺席', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          toast: {
            borderRadius: '6px',
            shadow: 's',
            variants: {
              default: { bg: '#a', text: '#b', border: '#c' },
              error: { bg: '#m', text: '#n', border: '#o' },
            },
          },
        },
      }),
    )
    expect(vars).toContain('--toast-default-bg: #a')
    expect(vars).toContain('--toast-error-border: #o')
    expect(vars).not.toContain('--toast-success-bg:')
    expect(vars).not.toContain('--toast-warning-bg:')
    expect(vars).not.toContain('--toast-info-bg:')
  })

  it('toast 全变体齐发；progress 无 variants 时仅结构变量', () => {
    const toastVars = compileThemeVariables(
      theme({
        components: {
          toast: {
            borderRadius: '6px',
            shadow: 's',
            variants: {
              default: { bg: '#a', text: '#b', border: '#c' },
              success: { bg: '#g', text: '#h', border: '#i' },
              error: { bg: '#m', text: '#n', border: '#o' },
              warning: { bg: '#j', text: '#k', border: '#l' },
              info: { bg: '#p', text: '#q', border: '#r' },
            },
          },
          progress: { borderRadius: '3px', trackBg: '#t' },
        },
      }),
    )
    expect(toastVars).toContain('--toast-success-text: #h')
    expect(toastVars).toContain('--toast-warning-border: #l')
    expect(toastVars).toContain('--toast-info-bg: #p')
    expect(toastVars).toContain('--progress-radius: 3px')
    expect(toastVars).toContain('--progress-track-bg: #t')
    expect(toastVars).not.toContain('--progress-default:')

    const progressVars = compileThemeVariables(
      theme({
        components: {
          progress: {
            borderRadius: '3px',
            trackBg: '#t',
            variants: { default: '#1', success: '#2', warning: '#3', error: '#4' },
          },
        },
      }),
    )
    expect(progressVars).toContain('--progress-default: #1')
    expect(progressVars).toContain('--progress-success: #2')
    expect(progressVars).toContain('--progress-warning: #3')
    expect(progressVars).toContain('--progress-error: #4')
  })

  it('toast 无 variants：仅结构变量（318）；变体缺 default（320）或缺 error（330）时对应组不发', () => {
    const noVariants = compileThemeVariables(
      theme({ components: { toast: { borderRadius: '6px', shadow: 's' } } }),
    )
    expect(noVariants).toContain('--toast-radius: 6px')
    expect(noVariants).not.toContain('--toast-default-bg:')

    const noDefault = compileThemeVariables(
      theme({
        components: {
          toast: {
            borderRadius: '6px', shadow: 's',
            variants: { error: { bg: '#m', text: '#n', border: '#o' } },
          },
        },
      }),
    )
    expect(noDefault).toContain('--toast-error-bg: #m')
    expect(noDefault).not.toContain('--toast-default-bg:')

    const noError = compileThemeVariables(
      theme({
        components: {
          toast: {
            borderRadius: '6px', shadow: 's',
            variants: { default: { bg: '#a', text: '#b', border: '#c' } },
          },
        },
      }),
    )
    expect(noError).toContain('--toast-default-bg: #a')
    expect(noError).not.toContain('--toast-error-bg:')
  })

  it('dialog 声明 overlayBg 但不可解析：遮罩仍回退深色半透明；可解析时按配置发射', () => {
    const invalid = compileThemeVariables(
      theme({ components: { dialog: { overlayBg: 'paint', overlayOpacity: 0.7 } } }),
    )
    expect(invalid).toContain('--overlay-bg: rgba(0, 0, 0, 0.5)')

    const valid = compileThemeVariables(
      theme({ components: { dialog: { overlayBg: '#010203', overlayOpacity: 0.7 } } }),
    )
    expect(valid).toContain('--overlay-bg: rgba(1, 2, 3, 0.7)')
  })

  it('dropdown/tabs/dialog 全声明逐项发射', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          dialog: { borderRadius: '8px', overlayBg: '#111', overlayOpacity: 0.4, shadow: 'ds', border: '1px solid' },
          tabs: { borderRadius: '6px', listBg: '#lb', activeBg: '#ab', activeText: '#at', inactiveText: '#it' },
          dropdownMenu: { borderRadius: '5px', shadow: 'dw', border: '#db', itemHoverBg: '#hb', itemHoverText: '#ht' },
        },
      }),
    )
    expect(vars).toContain('--tabs-inactive-text: #it')
    expect(vars).toContain('--dropdown-item-hover-bg: #hb')
    expect(vars).toContain('--dialog-shadow: ds')
  })
})

describe('compileThemeVariables — glow / ds 桥接 / 圆角阴影缺省面', () => {
  it('glow 缺席：不发射阴影辉光变量，--ds-accent-glow 回退主色', () => {
    const vars = compileThemeVariables(theme({ components: {} }))
    expect(vars).not.toContain('--shadow-glow-running:')
    expect(vars).not.toContain('--status-success-shadow:')
    expect(vars).toMatch(/--ds-accent-glow: [^\s]+/)
  })

  it('glow 声明：running/waiting 辉光 + 非强度键的状态阴影逐项发射', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          glow: { running: 'glow-r', waiting: 'glow-w', success: 'shadow-s', defaultGlowIntensity: 2 },
        },
      }),
    )
    expect(vars).toContain('--shadow-glow-running: glow-r')
    expect(vars).toContain('--shadow-glow-waiting: glow-w')
    expect(vars).toContain('--status-success-shadow: shadow-s')
    expect(vars).not.toContain('--status-default-glow-intensity:')
  })

  it('桥接 map 值为空串：该 --ds-* 变量跳过（不发空值）', () => {
    const vars = compileThemeVariables(
      theme({ colors: { text: { ...dark.colors.text, disabled: '' } } }),
    )
    expect(vars).not.toContain('--ds-text-disabled:')
    expect(vars).toContain('--ds-text-primary:')
  })

  it('borderRadius/shadows 缺席：--radius-* / --shadow-* 不发射；声明后按层级展开（default 键剔除）', () => {
    const absent = compileThemeVariables(theme({ components: {} }))
    expect(absent).not.toContain('--radius-sm:')
    expect(absent).not.toContain('--shadow-card-')

    const present = compileThemeVariables(
      theme({
        components: {
          borderRadius: { sm: '2px', md: '4px', lg: '8px', xl: '12px', defaultRadius: '6px' },
          shadows: {
            card: { sm: '0 0 2px', lg: '0 0 8px' },
            defaultShadow: { sm: 'hidden' },
          },
        },
      }),
    )
    expect(present).toContain('--radius-sm: 2px')
    expect(present).toContain('--radius-xl: 12px')
    expect(present).toContain('--shadow-card-sm: 0 0 2px')
    expect(present).toContain('--shadow-card-lg: 0 0 8px')
    expect(present).not.toContain('--shadow-defaultShadow-')
    expect(present).not.toContain('--radius-defaultRadius:')
  })
})

describe('compileThemeVariables — 字体/字号缺省面', () => {
  it('fonts 缺席：不发射 --font-ui/--font-code', () => {
    expect(compileThemeVariables(theme({ components: {} }))).not.toContain('--font-ui:')
  })

  it('fonts 声明 ui/code：双变量发射', () => {
    const vars = compileThemeVariables(
      theme({ components: { fonts: { ui: 'Segoe UI', code: 'JetBrains Mono' } } }),
    )
    expect(vars).toContain('--font-ui: Segoe UI')
    expect(vars).toContain('--font-code: JetBrains Mono')
  })

  it('fontSize 缺席：字号阶梯不发射；声明后 Tailwind 工具类与语义阶梯双套输出', () => {
    const absent = compileThemeVariables(theme({ components: {} }))
    expect(absent).not.toContain('--text-xs:')
    expect(absent).not.toContain('--font-size-body:')

    const fs = { xs: '11px', sm: '13px', md: '15px', lg: '18px', xl: '22px' }
    const vars = compileThemeVariables(theme({ components: { fontSize: fs } }))
    expect(vars).toContain('--text-xs: 11px')
    expect(vars).toContain('--text-xl: 22px')
    expect(vars).toContain('--text-base: 15px')
    expect(vars).toContain('--font-size-caption: 11px')
    expect(vars).toContain('--font-size-page-title: 22px')
  })
})

describe('compileThemeVariables — 区域背景回退与渐变双通道', () => {
  it('backgrounds 缺省：sidebar/chat 回退 colors.background 对应色', () => {
    const vars = compileThemeVariables(theme({ backgrounds: null }))
    expect(vars).toMatch(/--sidebar-bg: [^\s;]+/)
    expect(vars).toContain(`--sidebar-bg: ${dark.colors.background.sidebar}`)
    expect(vars).toContain(`--chat-bg: ${dark.colors.background.main}`)
    // 纯色：image 位 none、color 位取值
    expect(vars).toContain('--chat-bg-image: none')
  })

  it('区域色为空串：对应变量不发射（缺省面）', () => {
    const vars = compileThemeVariables(
      theme({
        colors: { background: { ...dark.colors.background, sidebar: '', main: '' } },
        backgrounds: null,
      }),
    )
    expect(vars).not.toContain('--sidebar-bg:')
    expect(vars).not.toContain('--chat-bg:')
  })

  it('backgrounds 声明渐变：chat 走 image 位、color 位 transparent', () => {
    const gradient = 'linear-gradient(180deg, #0B1220, #1E293B)'
    const vars = compileThemeVariables(
      theme({ backgrounds: { sidebar: { value: '#010203' }, chat: { value: gradient } } }),
    )
    expect(vars).toContain(`--chat-bg-image: ${gradient}`)
    expect(vars).toContain('--chat-bg-color: transparent')
    expect(vars).toContain('--sidebar-bg: #010203')
  })
})

describe('compileThemeVariables — effects 三档时长回退与归零', () => {
  it('effects 缺席：200ms 基准 + 默认缓动的三档回退', () => {
    const vars = compileThemeVariables(theme({ effects: {} }))
    expect(vars).toContain('--transition-easing: cubic-bezier(0.4, 0, 0.2, 1)')
    expect(vars).toContain('--transition-fast: 120ms cubic-bezier(0.4, 0, 0.2, 1)')
    expect(vars).toContain('--transition-base: 200ms cubic-bezier(0.4, 0, 0.2, 1)')
    expect(vars).toContain('--transition-slow: 300ms cubic-bezier(0.4, 0, 0.2, 1)')
  })

  it('animations=false：三档全部归零（无障碍主题关动画）；自定义时长/缓动生效', () => {
    const vars = compileThemeVariables(
      theme({ effects: { transitionDuration: 400, transitionEasing: 'linear', animations: false } }),
    )
    expect(vars).toContain('--transition-easing: linear')
    expect(vars).toContain('--transition-fast: 0ms linear')
    expect(vars).toContain('--transition-base: 0ms linear')
    expect(vars).toContain('--transition-slow: 0ms linear')
  })

  it('animations=true：按声明时长派生 0.6x/1x/1.5x', () => {
    const vars = compileThemeVariables(
      theme({ effects: { transitionDuration: 400, transitionEasing: 'linear', animations: true } }),
    )
    expect(vars).toContain('--transition-fast: 240ms linear')
    expect(vars).toContain('--transition-base: 400ms linear')
    expect(vars).toContain('--transition-slow: 600ms linear')
  })
})

describe('compileThemeVariables — 颜色解析容错（hex 前缀坏值 / rgba 透明度 / 渐变无色标）', () => {
  it('# 前缀但非合法 hex：HSL 桥接原样透传原值', () => {
    const vars = compileThemeVariables(
      theme({ colors: { text: { ...dark.colors.text, primary: '#GG1234' } } }),
    )
    expect(vars).toContain('--foreground: #GG1234')
  })

  it('rgba 带透明度：HSL 桥接输出 alpha 段（muted-foreground）；rgb 无透明度则无 alpha 段（1047 双面）', () => {
    const alphaVars = compileThemeVariables(
      theme({ colors: { text: { ...dark.colors.text, secondary: 'rgba(20, 30, 40, 0.5)' } } }),
    )
    const muted = alphaVars.match(/--muted-foreground: (\d+ \d+% \d+% \/ 0\.5)/)
    expect(muted).not.toBeNull()

    const noAlphaVars = compileThemeVariables(
      theme({ colors: { text: { ...dark.colors.text, secondary: 'rgb(20, 30, 40)' } } }),
    )
    expect(noAlphaVars).toMatch(/--muted-foreground: \d+ \d+% \d+%(?! \/)/)
  })

  it('panel-solid 用不透明通道：rgba 透明度被剥离、渐变无色标原样透传', () => {
    const solid = compileThemeVariables(
      theme({ colors: { background: { ...dark.colors.background, elevated: 'rgba(9, 8, 7, 0.9)' } } }),
    )
    const panel = solid.match(/--panel-solid: (\d+ \d+% \d+%)/)
    expect(panel).not.toBeNull()
    expect(solid).not.toContain('--panel-solid: rgba(9, 8, 7, 0.9)')

    const gradientNoStops = compileThemeVariables(
      theme({
        colors: {
          background: {
            ...dark.colors.background,
            elevated: 'linear-gradient(to right, red, blue)',
          },
        },
      }),
    )
    expect(gradientNoStops).toContain('--panel-solid: linear-gradient(to right, red, blue)')
  })
})

describe('applyTheme — 类目切换 / 空值变量 / 无主背景', () => {
  beforeEach(() => {
    document.documentElement.style.cssText = ''
    document.documentElement.className = ''
    document.body.style.background = ''
  })

  it('类目非 dark/light：不挂任何模式类（特殊类目主题）', () => {
    applyTheme(theme({ category: 'sepia' as ThemeConfig['category'] }))
    expect(document.documentElement.classList.contains('dark')).toBe(false)
    expect(document.documentElement.classList.contains('light')).toBe(false)
  })

  it('编译出的空值变量在应用时被跳过（status 色为空串 → 不写该 CSS 变量）', () => {
    applyTheme(
      theme({ colors: { status: { ...dark.colors.status, pending: '' } } }),
    )
    expect(document.documentElement.style.getPropertyValue('--status-pending')).toBe('')
    // 相邻非空变量仍正常写入
    expect(document.documentElement.style.getPropertyValue('--status-pending-foreground')).not.toBe('')
  })

  it('backgrounds.main 缺省：不写背景变量、body 背景不被触碰', () => {
    applyTheme(theme({ backgrounds: { main: undefined } as Record<string, unknown> }))
    expect(document.documentElement.style.getPropertyValue('--bg-main-gradient')).toBe('')
    expect(document.body.style.background).toBe('')
  })
})

describe('applyPluginThemeVars — 最小声明形态与变量生命周期', () => {
  beforeEach(() => {
    document.documentElement.style.cssText = ''
    document.body.className = ''
  })

  it('variables 含空值键：空值跳过、有效值写入（719 双面）', () => {
    applyPluginThemeVars(pluginTheme({ variables: { '--gap-ok': '1px', '--gap-bad': '' } }))
    const style = document.documentElement.style
    expect(style.getPropertyValue('--gap-ok')).toBe('1px')
    expect(style.getPropertyValue('--gap-bad')).toBe('')
  })

  it('无 backgrounds 声明：不动图片与纹理变量', () => {
    applyPluginThemeVars(pluginTheme({ variables: { '--only-var': 'v' } }))
    const style = document.documentElement.style
    expect(style.getPropertyValue('--bg-image')).toBe('')
    expect(style.getPropertyValue('--bg-texture')).toBe('')
  })

  it('image enabled 但缺 url：不加 has-bg-image 类、不写 --bg-image（732 缺省面）', () => {
    applyPluginThemeVars(pluginTheme({ backgrounds: { image: { enabled: true } } }))
    expect(document.body.classList.contains('has-bg-image')).toBe(false)
    expect(document.documentElement.style.getPropertyValue('--bg-image')).toBe('')
  })

  it('image 仅 url：只写 --bg-image，可选修饰键全缺省（734-738 缺省面）', () => {
    applyPluginThemeVars(pluginTheme({ backgrounds: { image: { url: 'https://x.example/a.png' } } }))
    const style = document.documentElement.style
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
    expect(style.getPropertyValue('--bg-image')).toBe('url(https://x.example/a.png)')
    expect(style.getPropertyValue('--bg-image-position')).toBe('')
    expect(style.getPropertyValue('--bg-image-size')).toBe('')
    expect(style.getPropertyValue('--bg-image-attachment')).toBe('')
    expect(style.getPropertyValue('--bg-overlay')).toBe('')
    expect(style.getPropertyValue('--bg-overlay-opacity')).toBe('')
  })

  it('texture enabled 但缺 type：不写任何纹理变量（747 缺省面）', () => {
    applyPluginThemeVars(pluginTheme({ backgrounds: { texture: { enabled: true } } }))
    expect(document.documentElement.style.getPropertyValue('--bg-texture')).toBe('')
  })

  it('切肤清理：上次皮肤发射的变量被移除，不漂移到新皮肤', () => {
    applyPluginThemeVars(pluginTheme({ variables: { '--skin-a': 'a' } }))
    expect(document.documentElement.style.getPropertyValue('--skin-a')).toBe('a')
    applyPluginThemeVars(pluginTheme({ variables: { '--skin-b': 'b' } }))
    expect(document.documentElement.style.getPropertyValue('--skin-a')).toBe('')
    expect(document.documentElement.style.getPropertyValue('--skin-b')).toBe('b')
  })
})
