/** @feature 双端优化轮 R25 | @ci: frontend-test */
/**
 * themeService — 镜像发射测试
 *
 * 一、-rgb 三元组镜像（R12 审计 P2 清理配套，R22 落地）：
 * 消费契约：半透明 tint/边框类以 rgb(var(--x-rgb,fallback)/alpha) 原生 alpha
 * 语法消费（Chromium 65+，无 color-mix 依赖）；主题引擎对相关源值随主变量
 * 同步发射 -rgb 三元组镜像：
 * - --code-bg（深色 rgba 串/浅色 hex 两路）→ --code-bg-rgb
 * - --badge-info-text（badge.info 变体在场时）→ --badge-info-text-rgb
 * 镜像通道必须与源值一致；源值缺席/不可解析时镜像不发，消费类内数值
 * fallback 兜底。
 *
 * 二、shadcn 桥变量 -t/-a alpha 镜像（R25 系统性收口配套）：
 * 主题源值 rgba(r,g,b,a) 经 colorToHsl 保留声明级 alpha（如 light 主题
 * `--border: 222 47% 11% / 0.1`），此类值下 hsl(var(--x)/N) 双 alpha 非法
 * （110 legacy 规则/手写包装 IACVT）。带 alpha 的桥值随主变量同步发射
 * -t（纯三元组）/-a（alpha 标量）镜像对，供 compat 因子化 legacy 规则与
 * 手写因子化声明消费；三元组值不发镜像（缺省回退等价直形态）。
 */

import { describe, expect, it, vi } from 'vitest'
import { presetThemes } from '@/config/themes'
import { compileThemeVariables } from '@/services/themeService'
import type { ThemeConfig } from '@/types/theme'

const { importTheme } = vi.hoisted(() => ({ importTheme: vi.fn() }))
vi.mock('@/services/themeStorage', () => ({
  ThemeStorageService: { importTheme },
}))

const dark = presetThemes['dark']

/** 浅拷贝 dark 并按域打补丁（cast 放宽：编译器对各域的缺省键本就逐项判空） */
function theme(patch: {
  colors?: Partial<ThemeConfig['colors']>
  components?: Record<string, unknown>
  category?: ThemeConfig['category']
} = {}): ThemeConfig {
  return {
    ...dark,
    ...(patch.category !== undefined ? { category: patch.category } : {}),
    colors: { ...dark.colors, ...patch.colors },
    components: {
      ...(patch.components ?? {}),
    } as ThemeConfig['components'],
    effects: dark.effects,
    backgrounds: dark.backgrounds,
  }
}

describe('compileThemeVariables — -rgb 三元组镜像发射（R22）', () => {
  it('深色 --code-bg（rgba 串）→ 镜像通道与源值一致', () => {
    const vars = compileThemeVariables(theme())
    expect(vars).toContain('--code-bg: rgba(15, 23, 42, 0.85)')
    expect(vars).toContain('--code-bg-rgb: 15 23 42')
  })

  it('浅色 --code-bg（hex）→ 镜像通道与源值一致（对齐画布类 fallback 常量）', () => {
    const vars = compileThemeVariables(
      theme({
        category: 'light',
        colors: { background: { ...dark.colors.background, input: '#1A1A2E' } },
      }),
    )
    expect(vars).toContain('--code-bg: #1A1A2E')
    expect(vars).toContain('--code-bg-rgb: 26 26 46')
  })

  it('badge.info 在场 → --badge-info-text-rgb 通道与源值一致', () => {
    const vars = compileThemeVariables(
      theme({
        components: {
          badge: {
            borderRadius: '4px',
            variants: { info: { bg: 'rgba(37, 99, 235, 0.12)', text: '#1D4ED8', border: 'rgba(37, 99, 235, 0.28)' } },
          },
        },
      }),
    )
    expect(vars).toContain('--badge-info-text: #1D4ED8')
    expect(vars).toContain('--badge-info-text-rgb: 29 78 216')
  })

  it('badge.info 缺席 → 镜像不发（消费类内数值 fallback 兜底）', () => {
    const vars = compileThemeVariables(theme({ components: {} }))
    expect(vars).not.toContain('--badge-info-text-rgb')
  })
})

describe('compileThemeVariables — 桥变量 -t/-a alpha 镜像发射（R25）', () => {
  it('浅色主题（现役）--border 源值 rgba(15,23,42,0.10) → -t 纯三元组 + -a 标量', () => {
    const vars = compileThemeVariables(presetThemes['light'])
    expect(vars).toContain('--border: 222 47% 11% / 0.1')
    expect(vars).toContain('--border-t: 222 47% 11%')
    expect(vars).toContain('--border-a: 0.1')
  })

  it('深色主题 --border 源值 rgba(148,163,184,0.12) → 镜像通道与 alpha 一致', () => {
    const vars = compileThemeVariables(dark)
    expect(vars).toContain('--border: 215 20% 65% / 0.12')
    expect(vars).toContain('--border-t: 215 20% 65%')
    expect(vars).toContain('--border-a: 0.12')
  })

  it('深空主题 --muted/--input 源值带 alpha → 各自镜像齐发（多 token 覆盖）', () => {
    const vars = compileThemeVariables(presetThemes['deep-space'])
    expect(vars).toContain('--muted: 193 74% 12% / 0.8')
    expect(vars).toContain('--muted-t: 193 74% 12%')
    expect(vars).toContain('--muted-a: 0.8')
    expect(vars).toContain('--input-t: 193 74% 12%')
    expect(vars).toContain('--input-a: 0.8')
  })

  it('三元组值（hex 源）→ 镜像不发（缺省回退等价直形态）', () => {
    const vars = compileThemeVariables(
      theme({ colors: { border: { ...dark.colors.border, default: '#334155' } } }),
    )
    expect(vars).toContain('--border: 215 25% 27%')
    // 精确到变量名+冒号：裸子串会被 --border-active 等同前缀变量误配
    expect(vars).not.toMatch(/--border-t:/)
    expect(vars).not.toMatch(/--border-a:/)
  })

  it('deep-space --card/--popover 同族镜像齐发（文本定位主变量行而非跨行误配）', () => {
    const vars = compileThemeVariables(presetThemes['deep-space'])
    // 深空 --card=rgba(16,27,32,0.85)/--popover=rgba(24,36,49,0.9)，通道各归各
    for (const [main, t] of [
      ['--card: ', '--card-t: '],
      ['--popover: ', '--popover-t: '],
    ] as const) {
      const mainLine = vars.split('; ').find((l) => l.startsWith(main))
      const tLine = vars.split('; ').find((l) => l.startsWith(t))
      expect(mainLine).toBeDefined()
      expect(tLine).toBeDefined()
      // -t 镜像 = 主变量剥 alpha 尾（性质断言：通道一致 + alpha 因子化）
      const m = mainLine!.match(/^(--[^:]+): (.+) \/ (\d+(?:\.\d+)?)$/)
      expect(m).not.toBeNull()
      expect(tLine).toBe(`${t}${m![2]}`)
    }
  })
})
