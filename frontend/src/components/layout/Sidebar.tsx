/**
 * 侧边栏组件
 * 显示会话列表和搜索功能
 *
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.3
 * - 9.1: 头部高度 56px (使用 CSS 变量 --sidebar-header-height)
 * - 9.2: 新建按钮尺寸 32px (sm)
 * - 9.3: 内边距 12px (使用 CSS 变量 --sidebar-padding)
 * - 9.4: 会话列表项高度 48px (使用 CSS 变量 --sidebar-item-height)
 * - 9.5: 搜索框高度 32px (使用 CSS 变量 --sidebar-search-height)
 * - 13.3: 新建会话时打开 Agent 选择模态框
 * - 新增: 创建会话后自动导航到主页面
 * - 新增: 移动端响应式支持
 */

import {
  Bell,
  ChatIcon,
  ChatActiveIcon,
  Loader2,
  Plus,
  User,
  X,
} from '@/assets/icons'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SessionEditModal } from '@/components/session/SessionEditModal'
import { SessionList } from '@/components/session/SessionList'
import { SessionSearch } from '@/components/session/SessionSearch'
import { NotificationCenter } from '@/components/chat/NotificationCenter'
import { ThemeButton } from '@/components/layout/ThemeButton'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { cn } from '@/lib/utils'
import { reportError } from '@/services/errorReporting'
import { searchGlobal, type SessionSearchHit, type MessageSearchHit } from '@/services/api/search'
import {
  openWorkspacePanel,
  openWorkspacePanelByPath,
} from '@/services/workspacePanelOpener'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { ContributionEntry } from '@/services/schema/ContributionRegistry'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAgentStore } from '@/stores/agentStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import type { Session } from '@/types'

/** fixed sessions + plugin container id */
type SidebarView = 'sessions' | string


interface SidebarProps {
  /** 是否为移动端 */
  isMobile?: boolean
}

/**
 * Deep Space v2 侧栏尺寸
 * 设计来源：画布 C · SideBar · 会话视图 (49:196)
 * - 宽度 288px，内边距 12px
 * - 头部 36px，搜索 32px，会话项 55px
 * - 折叠按钮放最顶部（用户决策）
 */
const SIDEBAR_STYLES = {
  headerHeight: 'h-9', // 36px
  padding: 'p-3', // 12px
  paddingX: 'px-3',
  buttonSize: 'sm' as const,
  searchHeight: 'h-8', // 32px
  itemHeight: 55,
  width: {
    desktop: 288,
    smallDesktop: 260,
    mobile: 288,
  },
} as const

/**
 * 侧边栏组件
 * 使用 memo 和 useMemo 优化性能
 *
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.3
 * - 新增: 创建会话后自动导航到会话页面
 * - 新增: 移动端响应式支持，带遮罩层和关闭按钮
 */
export const Sidebar = memo<SidebarProps>(({ isMobile = false }) => {
  const navigate = useNavigate()
  const [searchKeyword, setSearchKeyword] = useState('')
  /** 后端搜索结果（防抖调用 /ext/channel_api/search） */
  const [searchResults, setSearchResults] = useState<{
    sessions: SessionSearchHit[]
    messages: MessageSearchHit[]
  }>({ sessions: [], messages: [] })
  const [isSearching, setIsSearching] = useState(false)
  const [searchError, setSearchError] = useState(false)
  // 模态框统一状态: { mode: 'create' } 或 { mode: 'edit', sessionId } 或 null
  const [modal, setModal] = useState<{ mode: 'create' | 'edit'; sessionId?: string } | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const [activeView, setActiveView] = useState<SidebarView>('sessions')
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      const n = contributionRegistry.getViewsContainers().length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])
  const pluginContainers = useMemo(() => {
    void contribTick
    return contributionRegistry
      .getViewsContainers()
      .slice()
      .sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  }, [contribTick])
  const user = useAuthStore((s) => s.user)

  const sessions = useSessionStore((state) => state.sessions)
  const activeSessionId = useSessionStore((state) => state.activeSessionId)
  const deletingSessionIds = useSessionStore((state) => state.deletingSessionIds)
  const isLoading = useSessionStore((state) => state.isLoading)
  const createSession = useSessionListStore((state) => state.createSession)
  const setActiveSession = useSessionListStore((state) => state.setActiveSession)
  const deleteSession = useSessionListStore((state) => state.deleteSession)
  const copySession = useSessionListStore((state) => state.copySession)
  const toggleSessionStar = useSessionListStore((state) => state.toggleSessionStar)
  const toggleSessionPin = useSessionListStore((state) => state.toggleSessionPin)
  const renameSession = useSessionListStore((state) => state.renameSession)
  const updateSessionAgent = useSessionListStore((state) => state.updateSessionAgent)
  const fetchSessions = useSessionListStore((state) => state.fetchSessions)
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)
  const setSidebarCollapsed = useUIStore((state) => state.setSidebarCollapsed)
  // P5：通知唯一入口——底栏 Bell 绑定 notificationStore（与 NotificationCenter 同一 store）
  const toggleNotificationPanel = useNotificationStore((s) => s.togglePanel)
  const notificationUnreadCount = useNotificationStore(
    (s) => s.notifications.filter((n) => !n.isRead).length,
  )

  // Agent 数据统一在这里加载
  const fetchAgents = useAgentStore((state) => state.fetchAgents)

  // 等 auth token 就绪后加载数据，只加载一次
  const authToken = useAuthStore((state) => state.token)
  const hasLoadedRef = useRef(false)
  useEffect(() => {
    if (!authToken || hasLoadedRef.current) return
    hasLoadedRef.current = true
    fetchSessions().catch((error) => {
      reportError(error instanceof Error ? error.message : String(error), {
        type: 'server',
        componentName: 'Sidebar',
        operation: 'fetchSessions',
      })
    })
    fetchAgents().catch((error) => {
      reportError(error instanceof Error ? error.message : String(error), {
        type: 'server',
        componentName: 'Sidebar',
        operation: 'fetchAgents',
      })
    })
  }, [authToken])

  // 监听 WS session_update 事件，事件驱动刷新会话列表
  useEffect(() => {
    const handleSessionUpdate = () => {
      fetchSessions({ background: true }).catch(() => {})
    }
    globalWS.subscribe(WS_SERVER_EVENTS.SESSION_UPDATE, handleSessionUpdate)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.SESSION_UPDATE, handleSessionUpdate)
    }
  }, [fetchSessions])

  /**
   * 统一搜索：防抖调用后端搜索 API（/ext/channel_api/search）。
   * 输入停止 350ms 后发起请求；q 为空时清空结果。
   */
  useEffect(() => {
    const keyword = searchKeyword.trim()
    if (!keyword) {
      setSearchResults({ sessions: [], messages: [] })
      setSearchError(false)
      setIsSearching(false)
      return
    }
    setIsSearching(true)
    const timer = window.setTimeout(() => {
      searchGlobal(keyword, 'all', 20)
        .then((data) => {
          setSearchResults({
            sessions: data.sessions ?? [],
            messages: data.messages ?? [],
          })
          setSearchError(false)
        })
        .catch(() => {
          setSearchResults({ sessions: [], messages: [] })
          setSearchError(true)
        })
        .finally(() => setIsSearching(false))
    }, 350)
    return () => window.clearTimeout(timer)
  }, [searchKeyword])

  // 会话列表数据源——有后端搜索结果时用后端结果（会话命中），
  // 否则回退本地 sessions。消息搜索命中不改变会话列表（消息结果单独展示）。
  const filteredSessions = useMemo(() => {
    if (!searchKeyword.trim()) return sessions
    if (searchResults.sessions.length > 0) {
      // 后端返回的是会话 ID 列表，映射为完整 Session 对象（保留星标/置顶等本地状态）
      const hitIds = new Set(searchResults.sessions.map((h) => h.id))
      return sessions.filter((s) => hitIds.has(s.id))
    }
    // 后端无命中：返回空（展示"未找到匹配的会话"）
    return searchError ? sessions : []
  }, [searchKeyword, searchResults, sessions, searchError])

  /**
   * 处理会话点击 - 设置活动会话并导航到会话页面
   * Requirements: Requirement 2 - 点击会话可以从其他页面跳转到对话页面
   *
   * 切换会话前先调用 saveCurrentTabs() 持久化当前会话的 Tab 状态，避免标签数据丢失。
   */
  const handleSessionClick = useCallback(
    async (sessionId: string) => {
      // 切换前保存当前会话的 Tab 状态到 localStorage
      useAgentTabStore.getState().saveCurrentTabs()
      await setActiveSession(sessionId)
    },
    [setActiveSession],
  )

  /**
   * 打开新建会话模态框
   */
  const handleOpenNewSessionModal = useCallback(() => {
    setModal({ mode: 'create' })
  }, [])

  /**
   * 关闭模态框
   */
  const handleCloseModal = useCallback(() => {
    setModal(null)
  }, [])

  /**
   * 确认创建 / 编辑会话
   */
  const handleSaveSession = useCallback(
    async (sessionId: string | null, title: string, agentId: string | null) => {
      setIsSaving(true)
      try {
        if (sessionId) {
          // 编辑已有会话 — 两个操作必须串行，避免竞争
          await renameSession(sessionId, title)
          await updateSessionAgent(sessionId, agentId)
          setModal(null)
        } else {
          // 新建会话：createSession 内部已设置 activeSessionId，
          // ChatContainer 会随 activeSessionId 自动渲染，无需 navigate。
          const session = await createSession(title || undefined, {
            agentId: agentId || undefined,
          })
          setModal(null)
        }
      } catch (error) {
        reportError(error instanceof Error ? error.message : String(error), {
          type: 'server',
          componentName: 'Sidebar',
          operation: sessionId ? 'saveSessionEdit' : 'createSession',
          sessionId: sessionId || undefined,
        })
      } finally {
        setIsSaving(false)
      }
    },
    [createSession, renameSession, updateSessionAgent],
  )

  /**
   * 处理编辑会话 - 打开编辑模态框
   */
  const handleEditSession = useCallback((session: Session) => {
    setModal({ mode: 'edit', sessionId: session.id })
  }, [])

  /**
   * 处理复制会话
   */
  const handleCopySession = useCallback(
    async (session: Session) => {
      try {
        await copySession(session.id)
      } catch (error) {
        reportError(error instanceof Error ? error.message : String(error), {
          type: 'server',
          componentName: 'Sidebar',
          operation: 'copySession',
          sessionId: session.id,
        })
      }
    },
    [copySession],
  )

  /**
   * 处理星标会话
   */
  const handleStarSession = useCallback(
    (sessionId: string) => {
      toggleSessionStar(sessionId)
    },
    [toggleSessionStar],
  )

  /**
   * 处理置顶会话
   */
  const handlePinSession = useCallback(
    (sessionId: string) => {
      toggleSessionPin(sessionId)
    },
    [toggleSessionPin],
  )

  /**
   * P3: 重置消息 → 刷新整个前端页面（重新初始化全部 store，等价于整页刷新）
   * 会话切换（仅刷新消息窗口）由 ChatContainer 的 key={activeTabId || sessionId}
   * 机制 + setActiveSession 重新加载消息实现，此处仅处理"重置消息"整页刷新。
   */
  const handleResetMessages = useCallback((_sessionId: string) => {
    window.location.reload()
  }, [])

  /**
   * 获取正在编辑的会话
   */
  const editingSession = modal?.mode === 'edit' && modal.sessionId
    ? sessions.find((s) => s.id === modal.sessionId) || null
    : null

  /**
   * 处理移动端关闭侧边栏
   */
  const handleCloseSidebar = useCallback(() => {
    if (isMobile) {
      setSidebarCollapsed(true)
    }
  }, [isMobile, setSidebarCollapsed])

  /**
   * 处理会话点击（移动端自动关闭侧边栏）
   */
  const handleSessionClickMobile = useCallback(
    async (sessionId: string) => {
      await handleSessionClick(sessionId)
      if (isMobile) {
        setSidebarCollapsed(true)
      }
    },
    [handleSessionClick, isMobile, setSidebarCollapsed],
  )


  const handlePluginClick = useCallback(
    (entry: ContributionEntry) => {
      setActiveView(entry.id)
      if (entry.path && typeof entry.path === 'string') {
        const opened = openWorkspacePanelByPath(entry.path)
        if (!opened && entry.path.startsWith('/')) {
          navigate(entry.path)
        }
      } else if (entry.widget) {
        openWorkspacePanel({
          id: `ws-plugin-${entry.id}`,
          title: entry.title || entry.id,
          component: String(entry.widget),
          icon: entry.icon,
          moduleId: entry.pluginId ? `__plugin_${entry.pluginId}__` : `__contrib_${entry.id}__`,
        })
      } else {
        openWorkspacePanel({
          id: `ws-plugin-${entry.id}`,
          title: entry.title || entry.id,
          component: entry.id,
          icon: entry.icon,
          moduleId: `__contrib_${entry.id}__`,
        })
      }
      if (isMobile) setSidebarCollapsed(true)
    },
    [isMobile, navigate, setSidebarCollapsed],
  )

  const handleSessionsClick = useCallback(() => {
    setActiveView('sessions')
  }, [])


  return (
    <>
      {/* 移动端遮罩层 */}
      {isMobile && !sidebarCollapsed && (
        <div
          data-testid="sidebar-overlay"
          className="animate-in fade-in fixed inset-0 z-40 bg-black/50 duration-200"
          onClick={handleCloseSidebar}
          aria-hidden="true"
        />
      )}

      <aside
        data-testid="sidebar"
        className={cn(
          // 必须 h-full：父级是 Splitter 内 full-height panel；否则 flex-1 无效，底部空白
          'border-border/50 flex h-full min-h-0 flex-col border-r transition-all duration-300 ease-in-out',
          'bg-[var(--sidebar-bg-light)] dark:bg-[var(--sidebar-bg-dark)]',
          isMobile && !sidebarCollapsed && 'fixed top-0 left-0 z-50 shadow-2xl',
        )}
        style={
          sidebarCollapsed && !isMobile
            ? { width: 0, minWidth: 0, maxWidth: 0, flexShrink: 0, overflow: 'hidden', border: 'none', padding: 0 }
            : isMobile
              ? {
                  width: `${SIDEBAR_STYLES.width.mobile}px`,
                  minWidth: `${SIDEBAR_STYLES.width.mobile}px`,
                  maxWidth: `${SIDEBAR_STYLES.width.mobile}px`,
                  flexShrink: 0,
                }
              : {
                  width: '100%',
                  height: '100%',
                  minWidth: 0,
                  maxWidth: '100%',
                  minHeight: 0,
                  flexShrink: 0,
                  background: 'var(--ds-bg-panel, hsl(var(--card)))',
                }
        }
      >
        {/* ---- 折叠状态：图标菜单栏（48px，VS Code 感，非双侧栏） ---- */}
        {sidebarCollapsed && !isMobile ? (
          
          <div className="flex h-full flex-col items-center py-3" data-testid="sidebar-rail">
            <button
              type="button"
              onClick={handleSessionsClick}
              className={cn(
                'mb-2 flex h-10 w-10 items-center justify-center rounded-[10px] transition-colors',
                activeView === 'sessions'
                  ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
              )}
              style={
                activeView === 'sessions'
                  ? {
                      background: 'var(--ds-bg-elevated, #111C38)',
                      boxShadow:
                        'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
                    }
                  : undefined
              }
              title="会话"
              data-testid="sidebar-rail-sessions"
            >
              {activeView === 'sessions' ? (
                <ChatActiveIcon className="h-5 w-5" />
              ) : (
                <ChatIcon className="h-5 w-5" />
              )}
            </button>
            {pluginContainers.map((entry) => {
              const selected = activeView === entry.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => handlePluginClick(entry)}
                  className={cn(
                    'mb-2 flex h-10 w-10 items-center justify-center rounded-[10px] transition-colors',
                    selected
                      ? 'text-[var(--ds-accent-primary,#22D3EE)]'
                      : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                  )}
                  style={
                    selected
                      ? {
                          background: 'var(--ds-bg-elevated, #111C38)',
                          boxShadow:
                            'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))',
                        }
                      : undefined
                  }
                  title={entry.title || entry.id}
                  data-testid={`sidebar-rail-plugin-${entry.id}`}
                >
                  <span className="text-[14px]">{entry.icon || '◆'}</span>
                </button>
              )
            })}
            {/* 折叠态底部：仅用户 / 主题 / 通知（与展开态一致） */}
            <div className="mt-auto flex flex-col items-center gap-2 pb-1" data-testid="sidebar-rail-footer">
              <button
                type="button"
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 flex h-9 w-9 items-center justify-center rounded-[10px]"
                title={(user as { username?: string; email?: string } | null)?.username || '用户'}
                aria-label="用户"
              >
                <User className="h-4 w-4" />
              </button>
              <ThemeButton compact />
              <button
                type="button"
                onClick={toggleNotificationPanel}
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 relative flex h-9 w-9 items-center justify-center rounded-[10px]"
                title="通知"
                aria-label={`通知${notificationUnreadCount > 0 ? ` (${notificationUnreadCount} 条未读)` : ''}`}
                data-testid="sidebar-rail-notification"
              >
                <Bell className="h-4 w-4" />
                {notificationUnreadCount > 0 && (
                  <span className="absolute -top-0.5 -right-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[9px] font-bold text-white">
                    {notificationUnreadCount > 99 ? '99+' : notificationUnreadCount}
                  </span>
                )}
              </button>
            </div>
          </div>

        ) : (
          /* ---- 展开状态：WorkBuddy 风格纵向导航 + 会话列表 ---- */
          <>
            
            <div className="flex h-full min-h-0 flex-1 flex-col" data-testid="sidebar-main">
            <div className="flex flex-col gap-0.5 px-2 pt-3 pb-2" data-testid="sidebar-nav">
              <button
                type="button"
                onClick={handleOpenNewSessionModal}
                className="hover:bg-white/5 text-foreground mb-1 flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] font-medium transition-colors"
                data-testid="new-session-button"
              >
                <span
                  className="flex h-6 w-6 items-center justify-center rounded-md"
                  style={{ background: 'var(--ds-bg-elevated, #111C38)' }}
                >
                  <Plus className="h-3.5 w-3.5 text-[var(--ds-accent-primary,#22D3EE)]" />
                </span>
                新建会话
              </button>

              {/* plugin contributed viewsContainers (vscode-like) */}
              {pluginContainers.map((entry) => {
                const selected = activeView === entry.id
                return (
                  <button
                    key={entry.id}
                    type="button"
                    onClick={() => handlePluginClick(entry)}
                    className={cn(
                      'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors',
                      selected
                        ? 'text-foreground'
                        : 'text-muted-foreground hover:text-foreground hover:bg-white/5',
                    )}
                    style={
                      selected
                        ? {
                            background: 'var(--ds-bg-elevated, #111C38)',
                            boxShadow:
                              'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.35))',
                          }
                        : undefined
                    }
                    data-testid={`sidebar-menu-plugin-${entry.id}`}
                    title={entry.title || entry.id}
                  >
                    <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[13px] opacity-90">
                      {entry.icon || '◆'}
                    </span>
                    <span className="min-w-0 flex-1 truncate">{entry.title || entry.id}</span>
                  </button>
                )
              })}
            </div>

            <div className="border-border mx-2 mb-1 border-t" />

<div className="text-muted-foreground flex items-center justify-between px-3 py-1.5 text-[11px]">
              <span className="font-medium tracking-wide">
                会话{filteredSessions.length ? `(${filteredSessions.length})` : ''}
              </span>
              {isMobile && (
                <button
                  type="button"
                  onClick={handleCloseSidebar}
                  className="hover:text-foreground"
                  aria-label="关闭侧边栏"
                  data-testid="close-sidebar-button"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>

            {/* 搜索框区域（统一搜索：会话名 + 消息内容） */}
            <div
              className={cn('border-border/50 overflow-hidden border-b', SIDEBAR_STYLES.padding)}
              data-testid="sidebar-search-section"
            >
              <SessionSearch
                value={searchKeyword}
                onSearchChange={setSearchKeyword}
                resultCount={filteredSessions.length}
                totalCount={sessions.length}
                isSearching={isSearching}
                className="sidebar-search"
                inputClassName={SIDEBAR_STYLES.searchHeight}
              />
              {/* 消息搜索结果（有关键词时展示） */}
              {searchKeyword.trim() && searchResults.messages.length > 0 && (
                <div className="mt-1 space-y-0.5" data-testid="sidebar-message-results">
                  {searchResults.messages.slice(0, 8).map((hit) => (
                    <button
                      key={`${hit.session_id}-${hit.id}`}
                      type="button"
                      onClick={() => handleSessionClick(hit.session_id)}
                      className="text-muted-foreground hover:text-foreground hover:bg-white/5 block w-full truncate rounded px-2 py-1 text-left text-[11px] transition-colors"
                      title={hit.content}
                    >
                      {hit.content}
                    </button>
                  ))}
                </div>
              )}
              {searchError && (
                <p className="text-muted-foreground mt-1 px-1 text-[10px]">
                  搜索暂不可用，已展示全部会话
                </p>
              )}
            </div>

            {/* 会话列表 - Requirements: 9.3, 9.4 */}
            <div className="scrollbar-thin min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
              {isLoading ? (
                <div
                  className={cn(
                    'flex flex-col items-center text-center',
                    SIDEBAR_STYLES.padding,
                    'py-6',
                  )}
                >
                  <Loader2 className="text-muted-foreground mb-2 h-6 w-6 animate-spin" />
                  <p className="text-muted-foreground text-sm">加载中...</p>
                </div>
              ) : filteredSessions.length === 0 ? (
                <div
                  className={cn(
                    'flex flex-col items-center text-center',
                    SIDEBAR_STYLES.padding,
                    'py-6',
                  )}
                >
                  <p className="text-muted-foreground text-sm">
                    {searchKeyword ? '未找到匹配的会话' : '暂无会话'}
                  </p>
                </div>
              ) : (
                <SessionList
                  sessions={filteredSessions}
                  activeSessionId={activeSessionId}
                  deletingSessionIds={deletingSessionIds}
                  onSessionClick={handleSessionClickMobile}
                  onDeleteSession={deleteSession}
                  onEditSession={handleEditSession}
                  onCopySession={handleCopySession}
                  onStarSession={handleStarSession}
                  onPinSession={handlePinSession}
                  onResetMessages={handleResetMessages}
                  className="px-2"
                  itemHeight={SIDEBAR_STYLES.itemHeight}
                />
              )}
            </div>

            {/* 底栏：紧凑单行，不占大空白 */}
            <div
              className="border-border mt-auto flex h-9 shrink-0 items-center gap-0.5 border-t px-1.5"
              data-testid="sidebar-footer"
            >
              <div className="text-muted-foreground flex min-w-0 flex-1 items-center gap-1.5 px-1">
                <User className="h-3.5 w-3.5 shrink-0 opacity-80" />
                <span className="truncate text-[11px] leading-none">
                  {(user as { username?: string; email?: string } | null)?.username ||
                    (user as { username?: string; email?: string } | null)?.email ||
                    '未登录'}
                </span>
              </div>
              <ThemeButton compact />
              <button
                type="button"
                onClick={toggleNotificationPanel}
                className="text-muted-foreground hover:text-foreground hover:bg-white/5 relative flex h-7 w-7 shrink-0 items-center justify-center rounded-md"
                title="通知"
                aria-label={`通知${notificationUnreadCount > 0 ? ` (${notificationUnreadCount} 条未读)` : ''}`}
                data-testid="sidebar-notification"
              >
                <Bell className="h-3.5 w-3.5" />
                {notificationUnreadCount > 0 && (
                  <span className="absolute -top-1 -right-1 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-red-500 px-0.5 text-[9px] font-bold text-white">
                    {notificationUnreadCount > 99 ? '99+' : notificationUnreadCount}
                  </span>
                )}
              </button>
            </div>
            </div>
          </>
        )}
      </aside>
      {/* P5: 通知中心唯一入口承载——侧边栏 Bell 调用 togglePanel，面板/阻塞模态框在此挂载 */}
      <NotificationCenter hideTrigger />
      <SessionEditModal
        mode={modal?.mode || 'create'}
        isOpen={modal !== null}
        session={editingSession}
        onClose={handleCloseModal}
        onSave={handleSaveSession}
        isSaving={isSaving}
      />
    </>
  )
})
