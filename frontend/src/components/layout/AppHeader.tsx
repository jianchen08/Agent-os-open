/**
 * TitleBar · Deep Space v2 App Shell 顶栏
 *
 * 通用 AI 产品做法：
 * - 左侧：折叠侧边栏 + 品牌
 * - 中部：会话标题
 * - 右侧：功能入口（设置 / 监控）
 *
 * 用户 / 主题 / 通知 只放在侧栏底部，不在顶栏重复。
 */

import {
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRightIcon,
  MaximizeWindowIcon,
  RestoreWindowIcon,
} from '@/assets/icons'
import { useNavigate, useLocation } from 'react-router-dom'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { getTitleBarNavItems } from '@/constants/navItems'
import { cn } from '@/lib/utils'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'

/** AppHeader 属性 */
interface AppHeaderProps {
  /** 额外的右侧内容（如 pending 计数） */
  extraRight?: React.ReactNode
}

/**
 * 统一应用顶栏（设计稿 TitleBar）
 */
export function AppHeader({ extraRight }: AppHeaderProps) {
  const navigate = useNavigate()
  const location = useLocation()
  const navItems = getTitleBarNavItems()
  const sessions = useSessionStore((s) => s.sessions)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const activeSession = sessions.find((s) => s.id === activeSessionId)
  const sessionTitle = activeSession?.title || 'AgentOS'

  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const workspaceCollapsed = useUIStore((s) => s.workspaceCollapsed)
  const toggleWorkspace = useUIStore((s) => s.toggleWorkspace)
  const workspaceMaximized = useUIStore((s) => s.workspaceMaximized)
  const toggleWorkspaceMaximize = useUIStore((s) => s.toggleWorkspaceMaximize)

  const openNav = (path: string) => {
    const opened = openWorkspacePanelByPath(path)
    if (!opened) navigate(path)
  }

  return (
    <header
      className="border-border relative grid shrink-0 grid-cols-[1fr_auto_1fr] items-center border-b px-2 md:px-3"
      style={{
        height: 'var(--layout-titlebar-height, 32px)',
        background: 'var(--ds-bg-panel, hsl(var(--card)))',
      }}
      data-testid="app-header"
    >
      {/* 左侧 · 侧栏折叠 + 品牌 */}
      <div className="flex min-w-0 shrink-0 items-center gap-1.5 md:gap-2.5">
        <button
          type="button"
          onClick={toggleSidebar}
          className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors"
          title={sidebarCollapsed ? '展开侧边栏' : '隐藏侧边栏'}
          aria-label={sidebarCollapsed ? '展开侧边栏' : '隐藏侧边栏'}
          data-testid="titlebar-toggle-sidebar"
        >
          {sidebarCollapsed ? (
            <PanelLeftOpen className="h-4 w-4" />
          ) : (
            <PanelLeftClose className="h-4 w-4" />
          )}
        </button>

        <div
          className="h-[22px] w-[22px] shrink-0 rounded-md"
          style={{
            background: 'linear-gradient(135deg, #22D3EE 0%, #A78BFA 100%)',
          }}
          aria-hidden
        />
        <h1 className="text-foreground text-[13px] font-semibold leading-none">AgentOS</h1>
      </div>

      {/* 中部 · 会话标题 */}
      <div className="pointer-events-none flex items-center justify-center">
        <span className="text-muted-foreground max-w-[280px] truncate text-[12px]">
          {sessionTitle}
        </span>
      </div>

      {/* 右侧 · 功能入口（设置/监控）+ 工作区窗口控制 + 可选 extra */}
      <div className="flex shrink-0 items-center justify-end gap-2 md:gap-3">
        {extraRight}

        <nav className="hidden items-center gap-0.5 md:flex" data-testid="titlebar-nav">
          {navItems.map((item) => (
            <button
              key={item.path}
              type="button"
              onClick={() => openNav(item.path)}
              className={cn(
                'rounded px-1.5 py-0.5 text-[11px] transition-colors',
                location.pathname === item.path ||
                  location.pathname.startsWith(item.path + '/')
                  ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                  : 'text-muted-foreground hover:text-foreground',
              )}
              title={`打开「${item.label}」面板`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        {/* 工作区窗口控制（工作区在右侧，故控制按钮置右） */}
        <div className="flex items-center gap-0.5">
          {/* 工作区显隐（对齐 VS Code View: Toggle Panel） */}
          <button
            type="button"
            onClick={toggleWorkspace}
            className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors"
            title={workspaceCollapsed ? '显示工作区' : '隐藏工作区'}
            aria-label={workspaceCollapsed ? '显示工作区' : '隐藏工作区'}
            data-testid="titlebar-toggle-workspace"
          >
            <PanelRightIcon className="h-4 w-4" />
          </button>

          {/* 工作区最大化（保留顶栏/状态栏，仅折叠侧栏+聊天）。工作区隐藏时禁用 */}
          <button
            type="button"
            onClick={toggleWorkspaceMaximize}
            disabled={workspaceCollapsed}
            className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors disabled:pointer-events-none disabled:opacity-40"
            title={workspaceMaximized ? '还原最大化' : '最大化'}
            aria-label={workspaceMaximized ? '还原最大化' : '最大化'}
            data-testid="titlebar-toggle-maximize"
          >
            {workspaceMaximized ? (
              <RestoreWindowIcon className="h-4 w-4" />
            ) : (
              <MaximizeWindowIcon className="h-4 w-4" />
            )}
          </button>
        </div>

        {/* 移动端：设置/监控收进菜单 */}
        <div className="md:hidden">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground transition-colors"
                aria-label="导航菜单"
              >
                <Menu className="h-4 w-4" />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-40">
              {navItems.map((item) => (
                <DropdownMenuItem key={item.path} onClick={() => openNav(item.path)}>
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
