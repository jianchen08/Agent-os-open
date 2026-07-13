/**
 * 顶部导航栏组件
 * 显示系统标题、Logo、导航菜单、主题按钮、用户信息和用户菜单
 *
 * 布局结构：三区布局
 * - 左侧区域：Logo + 系统标题
 * - 中间区域：导航菜单
 * - 右侧区域：主题按钮 + 用户菜单
 *
 * Requirements: 4.1, 4.2, 4.3, 4.4, 4.5
 * 优化：使用现代细线条图标，增加圆角和过渡效果
 * 新增：移动端响应式支持，汉堡菜单和折叠导航
 */

import {
  Activity,
  Bot,
  Brain,
  Bug,
  Home,
  Menu,
  MessageSquare,
  Settings,
  Wrench,
  X,
} from 'lucide-react'
import React, { memo, useCallback, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { ROUTES } from '@/constants/routes'
import { cn } from '@/lib/utils'
import { useAuthStore } from '@/stores/authStore'
import { useUIStore } from '@/stores/uiStore'
import { ThemeButton } from './ThemeButton'
import { ThemePanel } from './ThemePanel'

interface TopNavProps {
  /** 是否为移动端 */
  isMobile?: boolean
}

/**
 * 导航菜单项配置
 */
export interface NavItem {
  path: string
  label: string
  icon: React.ComponentType<{ className?: string }> | null
}

/**
 * 导航菜单项列表 - 使用 Lucide 现代图标
 */
export const NAV_ITEMS: readonly NavItem[] = [
  { path: ROUTES.HOME, label: '主页', icon: Home },
  { path: ROUTES.TOOLS, label: '工具', icon: Wrench },
  { path: ROUTES.AGENTS, label: '智能体', icon: Bot },
  { path: ROUTES.MEMORY, label: '记忆', icon: Brain },
  { path: ROUTES.MONITORING, label: '监控', icon: Activity },
  { path: ROUTES.SETTINGS, label: '设置', icon: Settings },
  { path: ROUTES.DEBUG.ROOT, label: '调试', icon: Bug },
] as const

/**
 * 检查导航项是否处于激活状态
 * 支持精确匹配和子路由匹配
 * @param currentPath 当前 URL 路径
 * @param itemPath 导航项路径
 * @returns 是否激活
 */
export const isNavItemActive = (currentPath: string, itemPath: string): boolean => {
  // 精确匹配
  if (currentPath === itemPath) {
    return true
  }
  // 子路由匹配：当前路径以导航项路径开头（需要确保是路径边界）
  // 例如 /settings/theme 应该激活 /settings，但 /settings-other 不应该
  if (itemPath !== '/' && currentPath.startsWith(itemPath + '/')) {
    return true
  }
  return false
}

/**
 * 顶部导航栏组件
 * 使用 memo 和 useCallback 优化性能
 *
 * 响应式设计：
 * - 桌面端 (>1280px)：显示完整导航菜单
 * - 小桌面 (768px-1280px)：显示简化导航菜单（只显示图标）
 * - 移动端 (<=768px)：折叠导航菜单到汉堡菜单
 */
export const TopNav = memo<TopNavProps>(({ isMobile = false }) => {
  const navigate = useNavigate()
  const location = useLocation()
  const { user, logout } = useAuthStore()
  const toggleSidebar = useUIStore((state) => state.toggleSidebar)
  const [showUserMenu, setShowUserMenu] = useState(false)
  const [showThemePanel, setShowThemePanel] = useState(false)
  const [showMobileNav, setShowMobileNav] = useState(false)

  /**
   * 打开主题面板
   */
  const handleThemeButtonClick = useCallback(() => {
    setShowThemePanel((prev) => !prev)
    // 关闭用户菜单
    setShowUserMenu(false)
  }, [])

  /**
   * 关闭主题面板
   */
  const handleCloseThemePanel = useCallback(() => {
    setShowThemePanel(false)
  }, [])

  const handleLogout = useCallback(() => {
    logout()
    setShowUserMenu(false)
  }, [logout])

  /**
   * 启动悬浮窗 - 优先启动桌面应用，降级为浏览器弹窗
   */
  const handleOpenFloatingChat = useCallback(async () => {
    try {
      // 尝试通过后端 API 启动桌面悬浮窗
      const response = await fetch('/api/v1/floating-chat/launch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
      })

      if (response.ok) {
        return
      }

      // 如果后端返回 404，说明 Tauri 应用未编译，使用浏览器弹窗降级
    } catch (error) {
      // 无法连接后端，使用浏览器弹窗
    }

    // 降级方案：打开浏览器弹窗
    const currentPath = location.pathname
    let targetUrl = '/chat'
    if (currentPath.startsWith('/session/')) {
      targetUrl = currentPath
    }

    const floatingWindow = window.open(
      targetUrl,
      'floating-chat',
      'width=400,height=500,resizable=yes,scrollbars=no,status=no,menubar=no,toolbar=no,location=no,directories=no,top=100,left=100',
    )

    if (floatingWindow) {
      floatingWindow.focus()
    }
  }, [location.pathname])

  /**
   * 导航到指定页面
   */
  const handleNavigate = useCallback(
    (path: string) => {
      navigate(path)
      // 移动端导航后关闭菜单
      if (isMobile) {
        setShowMobileNav(false)
      }
    },
    [navigate, isMobile],
  )

  /**
   * 切换移动端导航菜单
   */
  const toggleMobileNav = useCallback(() => {
    setShowMobileNav((prev) => !prev)
  }, [])

  return (
    <header
      data-testid="top-nav"
      className="border-border/50 bg-background/95 flex h-10 items-center justify-between border-b px-4 backdrop-blur-sm"
      style={{ height: 'var(--topnav-height, 40px)' }}
    >
      {/* 左侧区域：侧边栏切换按钮 + 系统标题 */}
      <div className="flex items-center" style={{ gap: 'var(--spacing-3, 12px)' }}>
        {/* 侧边栏切换按钮 */}
        <Button
          variant="ghost"
          size="sm"
          onClick={toggleSidebar}
          className="hover:bg-muted/80 rounded-xl px-2 transition-all duration-200"
          title="切换侧边栏"
        >
          <Menu className="h-4 w-4" />
        </Button>

        {/* 系统标题 - 1280px 以下隐藏 */}
        <h1 className="text-foreground hidden text-lg font-semibold xl:block">智能体工作流系统</h1>
      </div>

      {/* 中间区域：导航菜单 - 桌面端显示完整菜单，小桌面显示简化菜单 */}
      {!isMobile && (
        <nav
          data-testid="top-nav-menu"
          className="flex items-center gap-1"
          role="navigation"
          aria-label="主导航"
        >
          {NAV_ITEMS.map((item) => {
            const isActive = isNavItemActive(location.pathname, item.path)
            const Icon = item.icon
            return (
              <Button
                key={item.path}
                data-testid={`nav-item-${item.path.replace(/\//g, '-').replace(/^-/, '')}`}
                data-active={isActive}
                onClick={() => handleNavigate(item.path)}
                aria-current={isActive ? 'page' : undefined}
                variant={isActive ? 'default' : 'ghost'}
                size="sm"
                className={`rounded-xl transition-all duration-200 ${
                  isActive
                    ? 'shadow-sm'
                    : 'hover:bg-muted/80 text-muted-foreground hover:text-foreground'
                } /* 大屏幕：显示完整按钮 */ /* 1280px 以下：只显示图标，减小间距 */ min-w-[72px] gap-1.5 lg:min-w-[72px] lg:gap-1.5`}
                title={item.label}
              >
                {Icon && <Icon className="h-4 w-4" />}
                {/* 1280px 以下隐藏文字，只显示图标 */}
                <span className="hidden text-sm xl:inline">{item.label}</span>
              </Button>
            )
          })}
        </nav>
      )}

      {/* 移动端：汉堡菜单按钮 */}
      {isMobile && (
        <Button
          variant="ghost"
          size="sm"
          onClick={toggleMobileNav}
          className={cn(
            'rounded-xl transition-all duration-200',
            showMobileNav ? 'bg-primary/20 text-primary' : 'hover:bg-muted/80',
          )}
          aria-label={showMobileNav ? '关闭导航菜单' : '打开导航菜单'}
          aria-expanded={showMobileNav}
        >
          {showMobileNav ? <X className="h-4 w-4" /> : <Menu className="h-4 w-4" />}
          <span className="ml-1 text-sm">菜单</span>
        </Button>
      )}

      {/* 右侧区域：悬浮窗按钮 + 主题按钮 + 用户菜单 */}
      <div className="flex items-center gap-1">
        {/* 悬浮窗启动按钮 */}
        <Button
          onClick={handleOpenFloatingChat}
          variant="ghost"
          size="icon"
          className="hover:bg-muted/80 h-8 w-8 rounded-lg transition-all duration-200"
          title="打开悬浮聊天窗口"
          aria-label="打开悬浮助手"
        >
          <MessageSquare className="h-4 w-4" />
        </Button>

        {/* 主题按钮和面板 */}
        <div className="relative">
          <ThemeButton onClick={handleThemeButtonClick} />
          <ThemePanel isOpen={showThemePanel} onClose={handleCloseThemePanel} />
        </div>

        {/* 用户信息和菜单 */}
        <div className="relative">
          {user ? (
            <div>
              {/* 用户信息按钮 */}
              <button
                data-testid="user-menu-button"
                onClick={() => setShowUserMenu(!showUserMenu)}
                className="hover:bg-muted/80 flex h-8 w-8 items-center justify-center rounded-lg transition-all duration-200 outline-none"
                style={{
                  border: 'none',
                  background: 'transparent',
                  overflow: 'hidden',
                }}
                title={user.username}
              >
                {/* 用户头像 */}
                <div
                  className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-full text-sm font-semibold"
                  style={{
                    backgroundColor: 'hsl(var(--primary))',
                    color: 'hsl(var(--primary-foreground))',
                  }}
                >
                  {user?.avatar ? (
                    <img
                      src={user.avatar}
                      alt={user.username}
                      className="h-full w-full object-cover"
                    />
                  ) : (
                    user?.username?.charAt(0).toUpperCase() || 'U'
                  )}
                </div>
              </button>

              {/* 用户菜单下拉框 - 优化样式 */}
              {showUserMenu && (
                <div
                  data-testid="user-menu-dropdown"
                  className="border-border/50 animate-in fade-in-0 zoom-in-95 absolute z-[100] mt-2 overflow-hidden rounded-xl border shadow-lg duration-200"
                  style={{
                    minWidth: '200px',
                    backgroundColor: 'hsl(var(--popover))',
                    right: '0',
                    top: '100%',
                  }}
                >
                  {/* 用户信息 */}
                  <div className="border-border/50 border-b px-4 py-3">
                    <p className="text-popover-foreground text-sm font-medium">{user.username}</p>
                    {user.email && <p className="text-muted-foreground text-xs">{user.email}</p>}
                  </div>

                  {/* 菜单项 */}
                  <div className="py-1">
                    {/* 登出 */}
                    <button
                      data-testid="logout-button"
                      onClick={handleLogout}
                      className="text-destructive hover:bg-destructive/10 mx-1 flex w-full items-center rounded-lg text-sm transition-colors"
                      style={{
                        height: 'var(--user-menu-item-height, 36px)',
                        paddingLeft: 'var(--user-menu-item-padding-x, 12px)',
                        paddingRight: 'var(--user-menu-item-padding-x, 12px)',
                        gap: 'var(--dropdown-item-gap, 8px)',
                        width: 'calc(100% - 8px)',
                      }}
                    >
                      <svg
                        className="h-4 w-4"
                        style={{
                          width: 'var(--user-menu-icon-size, 16px)',
                          height: 'var(--user-menu-icon-size, 16px)',
                        }}
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        strokeWidth={1.5}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1"
                        />
                      </svg>
                      退出登录
                    </button>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <Button
              variant="outline"
              size="sm"
              className="rounded-xl"
              onClick={() => navigate(ROUTES.LOGIN)}
            >
              登录
            </Button>
          )}
        </div>
      </div>

      {/* 移动端导航菜单下拉 */}
      {isMobile && showMobileNav && (
        <>
          {/* 遮罩层 */}
          <div
            data-testid="mobile-nav-overlay"
            className="animate-in fade-in fixed inset-0 z-40 bg-black/30 duration-200"
            onClick={() => setShowMobileNav(false)}
            aria-hidden="true"
          />
          {/* 导航菜单 */}
          <nav
            data-testid="mobile-nav-menu"
            className="bg-background/95 border-border/50 animate-in slide-in-from-top-2 absolute top-full right-0 left-0 z-50 border-b shadow-lg backdrop-blur-sm duration-200"
            role="navigation"
            aria-label="移动端主导航"
          >
            <div className="space-y-1 px-4 py-2">
              {NAV_ITEMS.map((item) => {
                const isActive = isNavItemActive(location.pathname, item.path)
                const Icon = item.icon
                return (
                  <Button
                    key={item.path}
                    data-testid={`mobile-nav-item-${item.path.replace(/\//g, '-').replace(/^-/, '')}`}
                    data-active={isActive}
                    onClick={() => handleNavigate(item.path)}
                    aria-current={isActive ? 'page' : undefined}
                    variant={isActive ? 'default' : 'ghost'}
                    size="sm"
                    className={cn(
                      'w-full justify-start gap-3 rounded-xl transition-all duration-200',
                      isActive
                        ? 'shadow-sm'
                        : 'hover:bg-muted/80 text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {Icon && <Icon className="h-4 w-4" />}
                    <span className="text-sm">{item.label}</span>
                  </Button>
                )
              })}
            </div>
          </nav>
        </>
      )}

      {/* 点击外部关闭菜单 */}
      {(showUserMenu || showThemePanel) && (
        <div
          data-testid="menu-overlay"
          className="fixed inset-0 z-40"
          onClick={() => {
            setShowUserMenu(false)
            setShowThemePanel(false)
          }}
        />
      )}
    </header>
  )
})

TopNav.displayName = 'TopNav'
