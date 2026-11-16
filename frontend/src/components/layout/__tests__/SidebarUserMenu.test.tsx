// @feature: FP-0.2.九 侧栏账号菜单与通知钮（Sidebar 克隆消除提取件） | @ci: frontend-test
/**
 * SidebarUserMenu / SidebarBellButton 契约测试
 *
 * 提取自 Sidebar 两处近全同复制后的行为冻结：双形态（rail/footer）菜单项
 * 集合与 testid 命名、认证态分叉（登录项 vs 修改密码/切换/退出）、工作区
 * 面板跳转接线（设置/监控/任务管理）、通知未读徽标 99+ 封顶。
 *
 * mock 边界：workspacePanelOpener（工作区面板跳转服务）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { SidebarBellButton, SidebarUserMenu } from '@/components/layout/SidebarUserMenu'
import {
  openDeclaredPluginPage,
  openWorkspacePanelByPath,
} from '@/services/workspacePanelOpener'

vi.mock('@/services/workspacePanelOpener', () => ({
  openWorkspacePanelByPath: vi.fn(),
  openDeclaredPluginPage: vi.fn(),
}))

/** Radix DropdownMenu 需要完整指针事件序列（pointerDown → pointerUp → click） */
function openDropdownMenu(trigger: HTMLElement): void {
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
}

describe('SidebarUserMenu 双形态账号菜单', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('footer 已登录：菜单项集合齐全，设置/任务管理走工作区面板跳转', async () => {
    const onLogout = vi.fn()
    render(
      <SidebarUserMenu
        variant="footer"
        isAuthenticated
        user={{ username: 'alice' }}
        onOpenLogin={vi.fn()}
        onOpenChangePassword={vi.fn()}
        onLogout={onLogout}
      />,
    )

    expect(screen.getByTestId('sidebar-user-area').textContent).toContain('alice')

    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    // 已登录 footer 形态：修改密码/切换/退出 + 设置/监控/任务管理 全集
    // （Radix portal 异步挂载，逐项 findBy 防时序竞态）
    expect(await screen.findByTestId('sidebar-user-menu-change-password')).toBeTruthy()
    expect(await screen.findByTestId('sidebar-user-menu-switch')).toBeTruthy()
    expect(await screen.findByTestId('sidebar-user-menu-logout')).toBeTruthy()

    fireEvent.click(await screen.findByTestId('sidebar-user-menu-settings'))
    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/settings')

    // 监控项：E6 声明面化——按声明引用开页（声明注册表活值解析，无路径词面）
    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-monitoring'))
    expect(openDeclaredPluginPage).toHaveBeenCalledWith(
      expect.objectContaining({ pluginId: 'monitoring', pageId: 'monitoring' }),
    )

    // Radix 点选菜单项后关闭菜单，重开再验任务管理跳转与退出回调
    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-tasks-2'))
    expect(openWorkspacePanelByPath).toHaveBeenCalledWith('/tasks')

    openDropdownMenu(screen.getByTestId('sidebar-user-area'))
    fireEvent.click(await screen.findByTestId('sidebar-user-menu-logout'))
    expect(onLogout).toHaveBeenCalledTimes(1)
  })

  it('rail 未登录：仅登录项无退出，点登录走 onOpenLogin', async () => {
    const onOpenLogin = vi.fn()
    render(
      <SidebarUserMenu
        variant="rail"
        isAuthenticated={false}
        user={null}
        onOpenLogin={onOpenLogin}
        onOpenChangePassword={vi.fn()}
        onLogout={vi.fn()}
      />,
    )

    openDropdownMenu(screen.getByTestId('sidebar-rail-user'))
    // rail 形态菜单项历史无 testid（组件契约），按文本定位登录项
    const loginItem = await screen.findByText('登录')
    expect(screen.queryByTestId('sidebar-user-menu-logout')).toBeNull()

    fireEvent.click(loginItem)
    expect(onOpenLogin).toHaveBeenCalledTimes(1)
  })
})

describe('SidebarBellButton 未读徽标', () => {
  it('0 未读无徽标；点击回调直达', () => {
    const onClick = vi.fn()
    render(<SidebarBellButton variant="footer" unreadCount={0} onClick={onClick} />)

    const btn = screen.getByTestId('sidebar-notification')
    expect(btn.textContent).not.toContain('99+')

    fireEvent.click(btn)
    expect(onClick).toHaveBeenCalledTimes(1)
  })

  it('150 未读显示 99+ 封顶（rail 形态带未读数 aria）', () => {
    render(<SidebarBellButton variant="rail" unreadCount={150} onClick={vi.fn()} />)

    const btn = screen.getByTestId('sidebar-rail-notification')
    expect(btn.getAttribute('aria-label')).toContain('150')
    expect(btn.textContent).toContain('99+')
  })
})
