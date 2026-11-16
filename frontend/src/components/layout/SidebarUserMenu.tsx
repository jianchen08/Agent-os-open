/**
 * 侧栏账号菜单与通知按钮——折叠 rail / 展开底栏双形态共用的提取件
 *
 * 两形态的菜单项集合、回调与样式逐字一致，差异仅：触发钮形态（图标钮 vs
 * 用户名行）、菜单弹出方向（right/top）、修改密码项（仅底栏）与 testid 命名
 * （rail 项无 testid，tasks 项历史命名 sidebar-user-menu-tasks / -tasks-2）。
 * 提取自 Sidebar 两处近全同复制（克隆消除顺带冻结基线回缩），行为零变化。
 */
import { Bell, User } from '@/assets/icons'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import { PLUGIN_DECLARED_PAGES } from '@/services/schema/pluginPages.generated'
import { openDeclaredPluginPage, openWorkspacePanelByPath } from '@/services/workspacePanelOpener'

type AuthUser = { username?: string; email?: string } | null

interface SidebarUserMenuProps {
  variant: 'rail' | 'footer'
  isAuthenticated: boolean
  user: AuthUser
  /** 登出进行中：菜单项禁用 +「退出中…」，防重复点击 */
  loggingOut?: boolean
  onOpenLogin: () => void
  onOpenChangePassword: () => void
  onLogout: () => void
}

/** 各形态的 testid 命名（rail 项除 tasks 外历史上无 testid，保持原样） */
const MENU_TESTIDS = {
  rail: {
    menu: 'sidebar-rail-user-menu',
    changePassword: undefined,
    login: undefined,
    switch: undefined,
    logout: undefined,
    settings: undefined,
    monitoring: undefined,
    tasks: 'sidebar-user-menu-tasks',
  },
  footer: {
    menu: 'sidebar-user-menu',
    changePassword: 'sidebar-user-menu-change-password',
    login: 'sidebar-user-menu-login',
    switch: 'sidebar-user-menu-switch',
    logout: 'sidebar-user-menu-logout',
    settings: 'sidebar-user-menu-settings',
    monitoring: 'sidebar-user-menu-monitoring',
    tasks: 'sidebar-user-menu-tasks-2',
  },
} as const

export function SidebarUserMenu({
  variant,
  isAuthenticated,
  user,
  loggingOut = false,
  onOpenLogin,
  onOpenChangePassword,
  onLogout,
}: SidebarUserMenuProps) {
  const rail = variant === 'rail'
  const ids = MENU_TESTIDS[variant]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {rail ? (
          <button
            type="button"
            className={cn(
              'flex h-9 w-9 items-center justify-center rounded-lg transition-colors outline-none data-[state=open]:bg-[var(--hover-overlay)]',
              isAuthenticated
                ? 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]'
                : 'text-primary hover:bg-[var(--hover-overlay)]',
            )}
            title={isAuthenticated ? `${user?.username || '用户'} · 点击展开` : '点击登录'}
            aria-label="账号菜单"
            data-testid="sidebar-rail-user"
          >
            <User className="h-4 w-4" />
          </button>
        ) : (
          <button
            type="button"
            className="text-muted-foreground hover:text-foreground flex min-w-0 flex-1 cursor-pointer items-center gap-1.5 rounded-md px-1 py-1 text-left transition-colors outline-none hover:bg-[var(--hover-overlay)] data-[state=open]:bg-[var(--hover-overlay)]"
            title={isAuthenticated ? `${user?.username || '用户'} · 点击展开` : '点击登录'}
            aria-label="账号菜单"
            data-testid="sidebar-user-area"
          >
            <User className="h-3.5 w-3.5 shrink-0 opacity-80" />
            <span className="truncate text-[11px] leading-none">{user?.username || user?.email || '未登录'}</span>
            {!isAuthenticated && (
              <span className="text-primary ml-auto shrink-0 text-[10px] font-medium">登录</span>
            )}
          </button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent
        side={rail ? 'right' : 'top'}
        align={rail ? 'center' : 'start'}
        sideOffset={6}
        className="w-48"
        data-testid={ids.menu}
      >
        <DropdownMenuLabel className="text-[11px]">
          {isAuthenticated ? user?.username || '已登录' : '未登录'}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {!isAuthenticated && (
          <DropdownMenuItem onClick={onOpenLogin} data-testid={ids.login}>
            登录
          </DropdownMenuItem>
        )}
        {isAuthenticated && (
          <>
            {variant === 'footer' && (
              <DropdownMenuItem onClick={onOpenChangePassword} data-testid={ids.changePassword}>
                修改密码
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={onOpenLogin} data-testid={ids.switch}>
              切换账号
            </DropdownMenuItem>
            <DropdownMenuItem
              onSelect={(e) => {
                // Radix select 默认关菜单——登出 await 窗口 preventDefault 留驻菜单，
                // 让「退出中…」+ 禁用真实可见（落定后成功跳转卸载/失败复位可重试）
                e.preventDefault()
                if (!loggingOut) onLogout()
              }}
              disabled={loggingOut}
              className="text-destructive focus:text-destructive disabled:cursor-not-allowed disabled:opacity-60"
              data-testid={ids.logout}
            >
              {loggingOut ? '退出中…' : '退出登录'}
            </DropdownMenuItem>
            <DropdownMenuSeparator />
          </>
        )}
        <DropdownMenuItem onClick={() => openWorkspacePanelByPath('/settings')} data-testid={ids.settings}>
          设置
        </DropdownMenuItem>
        <DropdownMenuItem
          onClick={() => openDeclaredPluginPage(PLUGIN_DECLARED_PAGES.monitoring.monitoring)}
          data-testid={ids.monitoring}
        >
          监控
        </DropdownMenuItem>
        <DropdownMenuItem onClick={() => openWorkspacePanelByPath('/tasks')} data-testid={ids.tasks}>
          任务管理
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

interface SidebarBellButtonProps {
  variant: 'rail' | 'footer'
  unreadCount: number
  onClick: () => void
}

/** 通知入口钮（未读徽标 + 99+ 封顶；rail/footer 仅尺寸与徽标偏移差异） */
export function SidebarBellButton({ variant, unreadCount, onClick }: SidebarBellButtonProps) {
  const rail = variant === 'rail'
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        'text-muted-foreground hover:text-foreground relative flex items-center justify-center hover:bg-[var(--hover-overlay)]',
        rail ? 'h-9 w-9 rounded-lg' : 'h-7 w-7 shrink-0 rounded-md',
      )}
      title="通知"
      aria-label={`通知${unreadCount > 0 ? ` (${unreadCount} 条未读)` : ''}`}
      data-testid={rail ? 'sidebar-rail-notification' : 'sidebar-notification'}
    >
      <Bell className={rail ? 'h-4 w-4' : 'h-3.5 w-3.5'} />
      {unreadCount > 0 && (
        <span
          className={cn(
            'bg-status-error text-status-error-foreground absolute flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-0.5 text-[9px] font-bold',
            rail ? '-top-0.5 -right-0.5' : '-top-1 -right-1',
          )}
        >
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
    </button>
  )
}
