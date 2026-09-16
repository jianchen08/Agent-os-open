/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ThemeButton / ThemePopover 覆盖缺口补测
 *
 * 覆盖契约：
 * - ThemeButton：单击在浅/深之间切换（resolvedTheme 决定方向）；悬停打开
 *   ThemePopover，移出后延迟 180ms 关闭（fake timers 断言 180ms 边界两侧）；
 *   快速移出再移入抵消关闭定时器（不关闭）；图标随 resolvedTheme 切换。
 * - ThemePopover：open=false 零渲染；主题列表来自 store（availableThemes 为空时
 *   回退内置深/浅两条）；点击卡片切换主题并请求关闭；当前主题显示选中勾；
 *   插件来源插件标注；无 preview 时按 category 回退静态近似色板。
 *
 * 测试策略：真实 zustand themeStore + 真实 Radix 无关的普通 DOM 组件；仅 mock
 * 主题服务（外部样式应用），断言用户可观察行为（回调 / 渲染文本 / store 状态）。
 */

import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ThemeButton } from '@/components/layout/ThemeButton'
import { ThemePopover } from '@/components/layout/ThemePopover'
import { useThemeStore } from '@/stores/themeStore'
import type * as themeServiceMod from '@/services/themeService'
import type { ThemeInfo } from '@/types/theme'

vi.mock('@/services/themeService', async (importOriginal) => {
  const actual = await importOriginal<typeof themeServiceMod>()
  return {
    ...actual,
    fetchDynamicThemes: vi.fn().mockResolvedValue([]),
    applyTheme: vi.fn(),
    applyPluginThemeVars: vi.fn(),
    clearPluginThemeVars: vi.fn(),
  }
})

const theme = (overrides: Partial<ThemeInfo> = {}): ThemeInfo => ({
  id: 'dark',
  name: '深色',
  category: 'dark',
  ...overrides,
})

describe('ThemeButton', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    useThemeStore.setState({
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
      availableThemes: [],
      isLoading: false,
      refreshThemes: vi.fn(),
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('深色下单击切换到浅色', () => {
    render(<ThemeButton />)
    fireEvent.click(screen.getByRole('button', { name: '切换主题' }))
    expect(useThemeStore.getState().mode).toBe('light')
  })

  it('浅色下单击切换到深色', () => {
    useThemeStore.setState({ mode: 'light', resolvedTheme: 'light' })
    render(<ThemeButton />)
    fireEvent.click(screen.getByRole('button', { name: '切换主题' }))
    expect(useThemeStore.getState().mode).toBe('dark')
  })

  it('悬停打开主题小窗，移出后 180ms 才关闭', () => {
    render(<ThemeButton />)
    fireEvent.mouseEnter(screen.getByTestId('theme-button-wrap'))
    expect(screen.getByTestId('theme-popover')).toBeInTheDocument()

    fireEvent.mouseLeave(screen.getByTestId('theme-button-wrap'))
    // 未到 180ms：仍在显示
    act(() => {
      vi.advanceTimersByTime(179)
    })
    expect(screen.getByTestId('theme-popover')).toBeInTheDocument()
    act(() => {
      vi.advanceTimersByTime(1)
    })
    expect(screen.queryByTestId('theme-popover')).toBeNull()
  })

  it('compact=false 时按钮使用大尺寸档（h-8 w-8）', () => {
    render(<ThemeButton compact={false} />)
    expect(screen.getByTestId('theme-button').className).toContain('h-8')
  })

  it('移出后 180ms 内再移入则取消关闭', () => {
    render(<ThemeButton />)
    const wrap = screen.getByTestId('theme-button-wrap')
    fireEvent.mouseEnter(wrap)
    fireEvent.mouseLeave(wrap)
    act(() => {
      vi.advanceTimersByTime(100)
    })
    fireEvent.mouseEnter(wrap)
    act(() => {
      vi.advanceTimersByTime(500)
    })
    expect(screen.getByTestId('theme-popover')).toBeInTheDocument()
  })
})

describe('ThemePopover', () => {
  beforeEach(() => {
    // refreshThemes 是外部主题服务边界：此处替换为 no-op，使 availableThemes
    // 保持测试注入值（真实 store 的其余行为不变）
    useThemeStore.setState({
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
      availableThemes: [],
      isLoading: false,
      refreshThemes: vi.fn(),
    })
  })

  it('open=false 时零渲染', () => {
    render(<ThemePopover open={false} />)
    expect(screen.queryByTestId('theme-popover')).toBeNull()
  })

  it('store 无主题时回退内置深/浅两条，点击切换并通知关闭', () => {
    const onOpenChange = vi.fn()
    render(<ThemePopover open onOpenChange={onOpenChange} />)
    expect(screen.getByText('深色')).toBeInTheDocument()
    expect(screen.getByText('浅色')).toBeInTheDocument()

    fireEvent.click(screen.getByTitle('深色'))
    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(useThemeStore.getState().currentThemeId).toBe('dark')
  })

  it('使用 store 主题列表并标注插件来源；当前主题有选中标记', () => {
    useThemeStore.setState({
      availableThemes: [
        theme({ id: 'ocean', name: 'Ocean', description: '海风主题', pluginId: 'theme_pack' }),
        theme({ id: 'light', name: '浅色', category: 'light' }),
      ],
      currentThemeId: 'ocean',
    })
    render(<ThemePopover open />)
    expect(screen.getByText('Ocean')).toBeInTheDocument()
    expect(screen.getByTitle('来源插件: theme_pack')).toBeInTheDocument()
    // 当前主题（ocean）行内存在选中勾（Check svg）
    const selected = screen.getByTitle('海风主题')
    expect(selected.querySelectorAll('svg').length).toBe(1)
    // 非选中主题行无勾
    expect(screen.getByTitle('浅色').querySelectorAll('svg').length).toBe(0)
  })

  it.each([
    ['plain-dark', 'dark', 'rgb(15, 23, 42)'],
    ['plain-light', 'light', 'rgb(248, 250, 252)'],
  ] as const)('无 preview 元数据时 %s/%s 回退静态色板', (id, category, expectedBg) => {
    useThemeStore.setState({
      availableThemes: [theme({ id, name: `${id} 名`, category })],
      currentThemeId: 'none',
    })
    render(<ThemePopover open />)
    const swatch = screen.getByTitle(`${id} 名`)
    const dot = swatch.querySelector('span[style]') as HTMLElement
    expect(dot.style.backgroundColor).toBe(expectedBg)
  })

  it('有 preview 元数据时用主题自带色板（优先于家族回退）', () => {
    useThemeStore.setState({
      availableThemes: [
        theme({
          id: 'ocean-breeze',
          name: '海风',
          category: 'dark',
          preview: { background: '#123456', primary: '#abcdef', text: '#111111', surface: '#222222', accent: '#333333' },
        }),
      ],
      currentThemeId: 'none',
    })
    render(<ThemePopover open />)
    const dot = screen.getByTitle('海风').querySelector('span[style]') as HTMLElement
    expect(dot.style.backgroundColor).toBe('rgb(18, 52, 86)')
  })

  it('点击非当前主题卡片后 store currentThemeId 更新', () => {
    useThemeStore.setState({
      availableThemes: [theme({ id: 'a', name: '甲' }), theme({ id: 'b', name: '乙' })],
      currentThemeId: 'a',
    })
    render(<ThemePopover open />)
    fireEvent.click(screen.getByTitle('乙'))
    expect(useThemeStore.getState().currentThemeId).toBe('b')
  })
})
