/**
 * AppHeader · 轻顶栏（AI app 标准，task_layout_responsive 任务 1）
 *
 * 三段式（≤44px）：
 * - 左：`☰` 侧栏入口（桌面折叠/展开，移动打开侧滑抽屉）
 * - 中：`灵汐 · 当前对话标题`（无标题显示品牌）+ 连接状态小圆点
 * - 右：高频动作——「工作区」按钮（桌面切换工作区显隐，移动端打开工作区全屏视图）
 *
 * 设计决策（调研固化）：
 * - 导航本体归侧栏（历史对话 + 设置/监控/插件 + 用户 + 新建对话），顶栏只是侧栏入口，不存在两套导航
 * - 去掉 MaximizeWindow/RestoreWindow 图标（用系统原生窗口控制）
 * - 右区仅保留一个高频动作「工作区」；extraRight prop 保留（pending 计数等挂右侧）
 */

import { Menu, PanelRightIcon } from '@/assets/icons'
import { useSessionsQuery } from '@/hooks/queries/useSessionsQuery'
import { ensureDefaultTaskPanel } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'

/** AppHeader 属性 */
interface AppHeaderProps {
  /** 额外的右侧内容（如 pending 计数） */
  extraRight?: React.ReactNode
  /** 移动端：点击「工作区」打开工作区全屏视图 */
  onOpenWorkspaceView?: () => void
  /** 是否为移动端形态（移动端「工作区」走 onOpenWorkspaceView） */
  isMobile?: boolean
}

/** 连接状态文案 */
const CONNECTION_LABEL: Record<string, string> = {
  connected: '内核已连接',
  connecting: '连接中…',
  reconnecting: '重连中…',
  disconnected: '连接已断开',
  failed: '连接失败',
}

/**
 * 统一应用轻顶栏（设计稿 TitleBar 精简版）
 */
export function AppHeader({ extraRight, onOpenWorkspaceView, isMobile = false }: AppHeaderProps) {
  const { data: sessions = [] } = useSessionsQuery()
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const activeSession = sessions.find((s) => s.id === activeSessionId)
  const sessionTitle = activeSession?.title
  // 中段上下文标识：`灵汐 · 会话标题`；无标题时仅品牌
  const title = sessionTitle ? `灵汐 · ${sessionTitle}` : '灵汐'

  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)
  const toggleSidebar = useUIStore((s) => s.toggleSidebar)
  const workspaceCollapsed = useUIStore((s) => s.workspaceCollapsed)
  const toggleWorkspace = useUIStore((s) => s.toggleWorkspace)
  const connectionStatus = useLayoutModeStore((s) => s.connectionStatus)

  const connected = connectionStatus.state === 'connected'
  const connecting =
    connectionStatus.state === 'connecting' || connectionStatus.state === 'reconnecting'
  const dotColor = connected
    ? 'var(--ds-status-success, #34D399)'
    : connecting
      ? 'var(--ds-status-waiting, #FBBF24)'
      : 'var(--ds-status-error, #F87171)'
  const connectionLabel = CONNECTION_LABEL[connectionStatus.state] ?? '未知状态'

  /** 面板入口：桌面切换显隐；移动端打开面板全屏视图。
   *  展开时确保默认「任务管理」标签存在（面板直接展示任务管理，非钉住可关闭）。 */
  const handleWorkspace = () => {
    if (isMobile && onOpenWorkspaceView) {
      onOpenWorkspaceView()
    } else {
      toggleWorkspace()
    }
    ensureDefaultTaskPanel()
  }

  return (
    <header
      className="border-border relative grid shrink-0 grid-cols-[1fr_auto_1fr] items-center border-b px-2 md:px-3"
      style={{
        height: 'var(--layout-titlebar-height, 44px)',
        background: 'var(--ds-bg-panel, hsl(var(--card)))',
      }}
      data-testid="app-header"
    >
      {/* 左侧 · 侧栏入口 ☰（桌面折叠/展开，移动打开抽屉） */}
      <div className="flex min-w-0 shrink-0 items-center gap-2">
        <button
          type="button"
          onClick={toggleSidebar}
          className="text-muted-foreground hover:bg-accent hover:text-foreground touch-expand flex h-7 w-7 items-center justify-center rounded-md transition-colors"
          title={sidebarCollapsed ? '展开侧边栏' : '隐藏侧边栏'}
          aria-label={sidebarCollapsed ? '展开侧边栏' : '隐藏侧边栏'}
          data-testid="titlebar-toggle-sidebar"
        >
          <Menu className="h-4 w-4" />
        </button>
      </div>

      {/* 中部 · 连接状态小圆点 + 上下文标识（灵汐 · 会话标题） */}
      <div className="flex min-w-0 items-center justify-center gap-1.5">
        <span
          className="inline-block h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: dotColor }}
          title={connectionLabel}
          aria-label={connectionLabel}
          data-testid="titlebar-connection-dot"
        />
        <span
          className="text-foreground min-w-0 truncate text-[13px] font-medium"
          data-testid="titlebar-title"
        >
          {title}
        </span>
      </div>

      {/* 右侧 · 高频动作：extra + 工作区 */}
      <div className="flex shrink-0 items-center justify-end gap-1 md:gap-2">
        {extraRight}

        <button
          type="button"
          onClick={handleWorkspace}
          className="text-muted-foreground hover:bg-accent hover:text-foreground touch-expand flex h-7 w-7 items-center justify-center rounded-md transition-colors"
          title={isMobile ? '打开面板' : workspaceCollapsed ? '显示面板' : '隐藏面板'}
          aria-label="面板"
          data-testid="titlebar-workspace"
        >
          <PanelRightIcon className="h-4 w-4" />
        </button>
      </div>
    </header>
  )
}
