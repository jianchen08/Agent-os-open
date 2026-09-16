/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * DebugUsersPage / ThemeSettingsPage 覆盖缺口补测
 *
 * DebugUsersPage 契约（useDebugUsersQuery 缓存链路）：
 * - 加载态 → LoadingState（无缓存首挂）
 * - 有数据：页面标题、用户总数、桌面表格与移动卡片两视图（用户名/角色/状态/
 *   创建时间/最后登录），禁用用户显示「禁用」、缺 last_login_at 显示「--」
 * - 空列表 → 「暂无数据」
 * - 错误（Error 实例 message / 非 Error 兜底文案）→ ErrorState
 * - embedded 透传 PageShell（无 header）
 *
 * ThemeSettingsPage 契约：
 * - 显示模式三按钮：light/dark/system 点击写 store.mode；
 * - 「当前解析为」文案随 resolvedTheme；
 * - 主题卡：store 列表优先、空列表回退静态 themeList；preview 四色片；
 *   插件来源标注；当前主题「✓ 当前使用」；category 三态文案（浅色/深色/特殊）；
 *   description 渲染；
 * - 选择主题卡调用 setTheme 与 refreshThemes；embedded 切换 PageShell 形态。
 */

import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'
import React from 'react'
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { DebugUsersPage } from '@/pages/debug/DebugUsersPage'
import { ThemeSettingsPage } from '@/pages/settings/ThemeSettingsPage'
import { getUsers } from '@/services/api/users'
import { queryClient } from '@/services/query/queryClient'
import { useThemeStore } from '@/stores/themeStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import type * as usersMod from '@/services/api/users'
import type { ThemeInfo } from '@/types/theme'

vi.mock('@/services/api/users', async (importOriginal) => {
  const actual = await importOriginal<typeof usersMod>()
  return { ...actual, getUsers: vi.fn() }
})

const mockGetUsers = vi.mocked(getUsers)

function user(overrides: Record<string, unknown> = {}) {
  return {
    id: 'u1',
    username: 'alice',
    role: 'admin',
    is_active: true,
    created_at: '2026-03-04T05:06:07Z',
    last_login_at: '2026-03-05T06:07:08Z',
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
})

describe('DebugUsersPage', () => {
  it('有数据：总数徽标与表格两视图字段齐全', async () => {
    mockGetUsers.mockResolvedValue([
      user({ id: 'a', username: 'alice', role: 'admin', is_active: true }),
      user({ id: 'b', username: 'bob', role: 'user', is_active: false, last_login_at: undefined }),
    ] as never)
    renderWithProviders(<DebugUsersPage />, { queryClient })

    await waitFor(() => {
      expect(screen.getByText('共 2 个用户')).toBeInTheDocument()
    })
    // 桌面表格 + 移动卡片各渲染一份（jsdom 不断言 CSS 显隐，按数量断言）
    expect(screen.getAllByText('alice')).toHaveLength(2)
    expect(screen.getAllByText('bob')).toHaveLength(2)
    expect(screen.getAllByText('禁用')).toHaveLength(2)
    // 缺 last_login_at：桌面表格独立单元格为 '--'，移动卡片为「最后登录：--」
    expect(screen.getAllByText(/--/)).toHaveLength(2)
    expect(
      within(document.querySelector('tbody') as HTMLElement)
        .getByText('alice')
        .closest('tr'),
    ).toHaveTextContent('admin')
  })

  it('空列表显示「暂无数据」', async () => {
    mockGetUsers.mockResolvedValue([] as never)
    renderWithProviders(<DebugUsersPage />, { queryClient })
    await waitFor(() => {
      expect(screen.getByText('暂无数据')).toBeInTheDocument()
    })
    expect(screen.getByText('共 0 个用户')).toBeInTheDocument()
  })

  it('Error 实例：错误 message 走 ErrorState', async () => {
    mockGetUsers.mockRejectedValue(new Error('后端 503') as never)
    renderWithProviders(<DebugUsersPage />, { queryClient })
    await waitFor(() => {
      expect(screen.getByText('后端 503')).toBeInTheDocument()
    })
  })

  it('非 Error 拒绝：显示兜底「获取用户列表失败」', async () => {
    mockGetUsers.mockRejectedValue('plain-string' as never)
    renderWithProviders(<DebugUsersPage />, { queryClient })
    await waitFor(() => {
      expect(screen.getByText('获取用户列表失败')).toBeInTheDocument()
    })
  })

  it('embedded 模式不渲染页面 header（标题不在标题标签中）', async () => {
    mockGetUsers.mockResolvedValue([user()] as never)
    renderWithProviders(<DebugUsersPage embedded />, { queryClient })
    await waitFor(() => {
      expect(screen.getByText('共 1 个用户')).toBeInTheDocument()
    })
    expect(screen.queryByRole('heading', { name: '用户调试' })).toBeNull()
  })

  it('点击行切换展开态（可折叠交互不抛错且保持列表可见）', async () => {
    mockGetUsers.mockResolvedValue([user()] as never)
    renderWithProviders(<DebugUsersPage />, { queryClient })
    await waitFor(() => {
      expect(screen.getByText('共 1 个用户')).toBeInTheDocument()
    })

    const table = document.querySelector('tbody') as HTMLElement
    const row = within(table).getByText('alice').closest('tr') as HTMLElement
    fireEvent.click(row)
    fireEvent.click(row)
    expect(within(table).getByText('alice')).toBeInTheDocument()

    // 移动端卡片（md:hidden 区块）的点击切换同样生效
    const mobileCards = document.querySelector('[class*="md:hidden"]')?.children ?? []
    expect(mobileCards).toHaveLength(1)
    fireEvent.click(mobileCards[0] as HTMLElement)
    fireEvent.click(mobileCards[0] as HTMLElement)
    expect(within(table).getByText('alice')).toBeInTheDocument()
  })
})

describe('ThemeSettingsPage', () => {
  const theme = (overrides: Partial<ThemeInfo> = {}): ThemeInfo => ({
    id: 'dark',
    name: '深色',
    category: 'dark',
    ...overrides,
  })

  beforeEach(() => {
    useThemeStore.setState({
      mode: 'dark',
      currentThemeId: 'dark',
      resolvedTheme: 'dark',
      availableThemes: [theme()],
      refreshThemes: vi.fn(),
    })
  })

  it.each([
    ['浅色', 'light'],
    ['深色', 'dark'],
    ['跟随系统', 'system'],
  ] as const)('显示模式按钮「%s」写 store.mode=%s', (label, mode) => {
    renderWithProviders(<ThemeSettingsPage embedded />)
    fireEvent.click(screen.getByRole('button', { name: label }))
    expect(useThemeStore.getState().mode).toBe(mode)
  })

  it('「当前解析为」文案随 resolvedTheme 呈现深/浅', () => {
    const { rerender } = renderWithProviders(<ThemeSettingsPage embedded />)
    expect(screen.getByText(/当前解析为：深色模式/)).toBeInTheDocument()

    useThemeStore.setState({ resolvedTheme: 'light' })
    rerender(<ThemeSettingsPage embedded />)
    expect(screen.getByText(/当前解析为：浅色模式/)).toBeInTheDocument()
  })

  it('store 列表为空时回退静态 themeList（多于两条预设）', () => {
    useThemeStore.setState({ availableThemes: [] })
    renderWithProviders(<ThemeSettingsPage embedded />)
    const cards = screen.getAllByRole('button', { name: /深色|浅色|深空|海洋|高对比|像素|软/ })
    expect(cards.length).toBeGreaterThan(2)
  })

  it('主题卡：preview 四色片、插件来源标注、当前使用标记、分类文案', () => {
    useThemeStore.setState({
      availableThemes: [
        theme({
          id: 'ocean',
          name: 'Ocean',
          description: '海风主题',
          pluginId: 'theme_pack',
          preview: {
            background: '#001122',
            primary: '#334455',
            text: '#ffffff',
            surface: '#223344',
            accent: '#556677',
          },
        }),
        theme({ id: 'plain-light', name: '素浅', category: 'light' }),
        theme({ id: 'special', name: '特殊主题', category: 'custom' }),
      ],
      currentThemeId: 'ocean',
    })
    renderWithProviders(<ThemeSettingsPage embedded />)

    // preview 四色片（title 标注）
    const swatch = (title: string) => (screen.getByTitle(title) as HTMLElement).style.backgroundColor
    expect(swatch('主色')).toBe('rgb(51, 68, 85)')
    expect(swatch('背景色')).toBe('rgb(0, 17, 34)')
    expect(swatch('表面色')).toBe('rgb(34, 51, 68)')
    expect(swatch('强调色')).toBe('rgb(85, 102, 119)')
    expect(screen.getByText('海风主题')).toBeInTheDocument()
    expect(screen.getByTitle('由插件 theme_pack 贡献')).toHaveTextContent('插件 · theme_pack')
    expect(screen.getByText('✓ 当前使用')).toBeInTheDocument()
    // 分类文案：浅色/深色/特殊（「浅色」同时命中显示模式按钮，限定到主题卡内断言）
    expect(within(screen.getByTitle('由插件 theme_pack 贡献').closest('button') as HTMLElement).getByText('深色')).toBeInTheDocument()
    expect(screen.getAllByText('浅色').length).toBeGreaterThanOrEqual(2)
    expect(screen.getByText('特殊')).toBeInTheDocument()
  })

  it('无 preview 时不渲染四色片', () => {
    useThemeStore.setState({ availableThemes: [theme({ id: 'nop', name: '无预览' })] })
    renderWithProviders(<ThemeSettingsPage embedded />)
    expect(screen.queryByTitle('主色')).toBeNull()
    expect(screen.getByText('无预览')).toBeInTheDocument()
  })

  it('点击非当前主题卡切换主题并刷新列表', () => {
    useThemeStore.setState({
      availableThemes: [theme({ id: 'a', name: '甲' }), theme({ id: 'b', name: '乙' })],
      currentThemeId: 'a',
    })
    renderWithProviders(<ThemeSettingsPage embedded />)

    fireEvent.click(screen.getByRole('button', { name: /乙/ }))
    expect(useThemeStore.getState().currentThemeId).toBe('b')
    expect(useThemeStore.getState().refreshThemes).toHaveBeenCalled()
  })

  it('非 embedded 模式渲染返回设置链接（title 与返回文案）', () => {
    renderWithProviders(<ThemeSettingsPage />)
    expect(screen.getByRole('heading', { name: '主题设置' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /返回设置/ })).toHaveAttribute('href', '/settings')
  })

  it('无 description 的卡片不渲染描述段', () => {
    useThemeStore.setState({ availableThemes: [theme({ id: 'nodesc', name: '无描述' })] })
    renderWithProviders(<ThemeSettingsPage embedded />)
    const card = screen.getByRole('button', { name: /无描述/ })
    expect(within(card).queryByText(/./, { selector: 'p' })).toBeNull()
  })
})
