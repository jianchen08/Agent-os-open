// @feature: 统一登出编排 | @ci: frontend-test
/**
 * Sidebar 登出菜单 → performLogout 统一编排测试
 *
 * 契约：侧栏「退出登录」（折叠/展开两种侧栏形态）走 services/auth/logout.ts
 * 的 performLogout（WS/流式清理+logout+跳转），与 router.tsx 的 onLogout
 * 同源——核心断言 globalWS.disconnect 被调用（裸 logout() 登出路径已废除，
 * 那条路径会漏掉 WS 断开，连接带旧 token 存活到心跳死亡）。
 *
 * 网络层 mock 边界：services/api/auth（logout 接口）与会话列表接口。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAuthStore } from '@/stores/authStore'
import { createTestQueryClient, renderWithProviders } from '@/test/renderWithProviders'

vi.mock('@/services/api/auth', () => ({
  login: vi.fn(),
  register: vi.fn(),
  refreshToken: vi.fn(),
  logout: vi.fn().mockResolvedValue(undefined),
  getCurrentUser: vi.fn(),
  changePassword: vi.fn(),
}))

// logout 内部动态 import 真实 GrowthLoop 会拉起重量依赖链（既有 authStore 测试同款 mock 边界）
vi.mock('@/services/modules/GrowthLoop', () => ({
  restartGrowthLoop: vi.fn().mockResolvedValue(undefined),
  destroyGrowthLoop: vi.fn(),
  initializeGrowthLoop: vi.fn().mockResolvedValue(undefined),
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api/session')>()
  return {
    ...actual,
    getSessions: vi.fn().mockResolvedValue([]),
  }
})

/** Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click） */
function openDropdownMenu(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

describe('Sidebar 登出走统一 performLogout', () => {
  let disconnectSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    vi.clearAllMocks()
    disconnectSpy = vi.spyOn(globalWS, 'disconnect').mockImplementation(() => {})
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

  it('展开侧栏用户菜单点「退出登录」→ globalWS.disconnect 被调用且认证态清除', async () => {
    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })

    openDropdownMenu(await screen.findByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-logout'))

    await waitFor(() => {
      expect(disconnectSpy).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
      expect(useAuthStore.getState().token).toBeNull()
    })
  })

  it('折叠侧栏用户菜单点「退出登录」→ 同样断开 WS', async () => {
    const { useUIStore } = await import('@/stores/uiStore')
    useUIStore.setState({ sidebarCollapsed: true })

    renderWithProviders(<Sidebar />, { queryClient: createTestQueryClient() })

    openDropdownMenu(await screen.findByTestId('sidebar-rail-user'))
    fireEvent.click(await screen.findByText('退出登录'))

    await waitFor(() => {
      expect(disconnectSpy).toHaveBeenCalledTimes(1)
    })
    await waitFor(() => {
      expect(useAuthStore.getState().isAuthenticated).toBe(false)
    })
  })
})
