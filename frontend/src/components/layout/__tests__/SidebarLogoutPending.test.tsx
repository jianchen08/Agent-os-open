// @feature FP-T12 前端适配（OBS-R258-1 交互反馈即时性收口） | @ci: frontend-test
/**
 * Sidebar 登出 pending 反馈测试（#7）：
 * - performLogout await 窗口：菜单项禁用 +「退出中…」（防重复点击）
 * - 落定后复位（成功路径页面跳转卸载；失败路径可重试）
 *
 * performLogout 为导航编排外部依赖，mock 可控 promise 模拟 await 窗口。
 */
import { fireEvent, screen, waitFor, cleanup } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { useAuthStore } from '@/stores/authStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'
import type * as SessionApiModule from '@/services/api/session'

const logoutMocks = vi.hoisted(() => ({ performLogout: vi.fn() }))
vi.mock('@/services/auth/logout', () => logoutMocks)

vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: vi.fn(),
  logout: vi.fn().mockResolvedValue(undefined),
  getCurrentUser: vi.fn(),
  changePassword: vi.fn(),
}))

// 登出链路外的重依赖（既有 SidebarLogout 测试同款 mock 边界）
vi.mock('@/services/modules/GrowthLoop', () => ({
  restartGrowthLoop: vi.fn().mockResolvedValue(undefined),
  destroyGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn().mockResolvedValue(undefined),
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<typeof SessionApiModule>()
  return {
    ...actual,
    getSessions: vi.fn().mockResolvedValue([]),
  }
})

/** Radix DropdownMenu 需要完整指针事件序列 */
function openDropdownMenu(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

beforeEach(() => {
  vi.clearAllMocks()
  useAuthStore.setState({
    isAuthenticated: true,
    user: {
      id: 'user-1',
      username: 'alice',
      email: 'alice@example.com',
      role: 'admin',
      createdAt: '2026-01-01T00:00:00Z',
    },
    token: 'test-token',
    mustChangePassword: false,
  })
})

afterEach(() => {
  cleanup()
})

describe('Sidebar 登出 pending（菜单项禁用 + 退出中…）', () => {
  it('performLogout await 窗口：退出菜单项 disabled +「退出中…」；落定复位', async () => {
    let resolveLogout: () => void = () => {}
    logoutMocks.performLogout.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveLogout = resolve
      }),
    )
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })

    openDropdownMenu(await screen.findByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-logout'))

    // await 窗口内：菜单项文案切换且禁用（Radix 禁用项以 data-disabled/aria-disabled 表达）
    await screen.findByText('退出中…')
    const item = screen.getByTestId('sidebar-user-menu-logout')
    expect(item).toHaveAttribute('aria-disabled', 'true')
    expect(logoutMocks.performLogout).toHaveBeenCalledTimes(1)
    fireEvent.click(item)
    expect(logoutMocks.performLogout).toHaveBeenCalledTimes(1)

    resolveLogout()
    await waitFor(() =>
      expect(screen.getByTestId('sidebar-user-menu-logout')).not.toHaveAttribute('aria-disabled'),
    )
    expect(screen.getByText('退出登录')).toBeInTheDocument()
  })

  it('performLogout 失败：复位可重试（不永久卡死在退出中）', async () => {
    logoutMocks.performLogout.mockRejectedValueOnce(new Error('logout boom'))
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })

    openDropdownMenu(await screen.findByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-logout'))

    await waitFor(() =>
      expect(screen.getByTestId('sidebar-user-menu-logout')).not.toHaveAttribute('aria-disabled'),
    )
    expect(screen.getByText('退出登录')).toBeInTheDocument()
  })
})
