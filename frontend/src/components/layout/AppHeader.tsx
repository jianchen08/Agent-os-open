/**
 * 统一应用导航栏
 *
 * 所有布局模式共用的顶部导航栏，确保切换布局时导航栏不变
 * 三区布局：左侧(sidebar toggle + 标题) | 中间(导航按钮 + 主题) | 右侧(连接状态 + 布局切换)
 *
 * 移动端适配：
 * - < md 断点下隐藏中间导航按钮区域，改为汉堡菜单下拉
 * - 右侧连接状态改为紧凑模式
 * - 布局切换按钮只显示图标
 */

import { PanelLeftClose, PanelLeftOpen, LayoutGrid, Menu, LogOut } from 'lucide-react'
import React from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ROUTES } from '@/constants/routes'
import { useAuthStore } from '@/stores/authStore'
import { useUIStore } from '@/stores/uiStore'
import { ConnectionStatusIndicator } from './ConnectionStatusIndicator'
import { ThemeButton } from './ThemeButton'
import { ThemePanel } from './ThemePanel'

/** 导航项定义 */
const NAV_ITEMS = [
  { path: ROUTES.TOOLS, label: '工具' },
  { path: ROUTES.AGENTS, label: '智能体' },
  { path: ROUTES.MONITORING, label: '监控' },
  { path: ROUTES.MEMORY, label: '记忆' },
  { path: ROUTES.SETTINGS, label: '设置' },
  { path: ROUTES.DEBUG.ROOT, label: '调试' },
] as const

/** AppHeader 属性 */
interface AppHeaderProps {
  /** 布局切换回调 */
  onToggleMode: () => void
  /** 当前布局模式标签 */
  modeLabel: string
  /** 是否显示主题面板 */
  showThemePanel: boolean
  /** 主题面板开关 */
  onShowThemePanel: (show: boolean) => void
  /** 登出回调 */
  onLogout: () => void
  /** 额外的右侧内容（如 pending 计数等） */
  extraRight?: React.ReactNode
}

/**
 * 统一应用导航栏组件
 *
 * 使用 CSS Grid 三列布局固定各区域位置
 * 中间导航区域用 pointer-events 穿透避免遮挡左右按钮
 *
 * 移动端（< md）：隐藏中间导航区，改为右侧汉堡菜单下拉
 * 桌面端（>= md）：保持原有三栏布局
 */
export function AppHeader({
  onToggleMode,
  modeLabel,
  showThemePanel,
  onShowThemePanel,
  onLogout,
  extraRight,
}: AppHeaderProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const user = useAuthStore((s) => s.user)

  return (
    <header className="border-border relative grid h-10 shrink-0 grid-cols-[auto_1fr_auto] items-center border-b px-2 md:px-3">
      {/* 左侧: sidebar toggle + 标题 */}
      <div className="flex shrink-0 items-center gap-2">
        <button
          onClick={toggleSidebar}
          className="hover:bg-accent rounded p-1 transition-colors"
          title={sidebarCollapsed ? '显示侧边栏' : '隐藏侧边栏'}
        >
          {sidebarCollapsed ? (
            <PanelLeftOpen className="h-4 w-4" />
          ) : (
            <PanelLeftClose className="h-4 w-4" />
          )}
        </button>
        <h1 className="text-sm font-semibold">SuperTerminal</h1>
      </div>

      {/* 中间: 导航按钮 + 主题 —— 仅桌面端显示 */}
      <div className="pointer-events-none hidden items-center justify-center md:flex">
        <div className="pointer-events-auto flex items-center gap-1">
          <nav className="flex min-w-0 items-center gap-1 overflow-x-auto">
            {NAV_ITEMS.map((item) => (
              <Button
                key={item.path}
                onClick={() => navigate(item.path)}
                variant={location.pathname === item.path ? 'default' : 'outline'}
                size="sm"
                className="rounded-md transition-all duration-200"
              >
                {item.label}
              </Button>
            ))}
          </nav>
          <div className="relative ml-2">
            <ThemeButton onClick={() => onShowThemePanel(true)} />
            {/* 主题面板：挂在 relative 锚点内，桌面端才能用 right-0/top-full 对齐到按钮下方。
                移动端为 fixed 底部抽屉，挂载位置不影响其定位。 */}
            <ThemePanel isOpen={showThemePanel} onClose={() => onShowThemePanel(false)} />
          </div>
        </div>
      </div>

      {/* 右侧: 连接状态 + 额外内容 + 布局切换 + 移动端汉堡菜单 */}
      <div className="flex shrink-0 items-center gap-1 md:gap-2">
        {/* 连接状态：桌面端完整模式，移动端紧凑模式 */}
        <div className="hidden md:block">
          <ConnectionStatusIndicator compact={false} showLatency showQueue />
        </div>
        <div className="md:hidden">
          <ConnectionStatusIndicator compact={true} />
        </div>

        {extraRight}

        {/* 移动端主题快速切换 —— 直接显示在顶部栏 */}
        <div className="md:hidden">
          <ThemeButton onClick={() => onShowThemePanel(true)} />
        </div>

        {/* 布局切换按钮 */}
        <button
          onClick={onToggleMode}
          className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-1.5 py-1 text-xs transition-colors md:px-2"
          title={`切换到${modeLabel}布局`}
        >
          <LayoutGrid className="h-3.5 w-3.5" />
          <span className="hidden sm:inline">{modeLabel}</span>
        </button>

        {/* 用户名 + 登出 */}
        {user && (
          <>
            <span className="text-muted-foreground hidden text-sm sm:inline">{user.username}</span>
            <button
              onClick={onLogout}
              className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-1.5 py-1 text-xs transition-colors"
              title="退出登录"
            >
              <LogOut className="h-3.5 w-3.5" />
            </button>
          </>
        )}

        {/* 移动端汉堡菜单 —— 仅移动端显示 */}
        <div className="md:hidden">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                className="hover:bg-accent rounded p-1 transition-colors"
                aria-label="导航菜单"
              >
                <Menu className="h-4 w-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-48">
              {NAV_ITEMS.map((item) => (
                <DropdownMenuItem
                  key={item.path}
                  onClick={() => navigate(item.path)}
                  className={location.pathname === item.path ? 'bg-accent font-medium' : ''}
                >
                  {item.label}
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </header>
  )
}
