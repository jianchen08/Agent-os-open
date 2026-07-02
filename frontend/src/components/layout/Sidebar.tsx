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

import { ChevronLeft, ChevronRight, Loader2, MessageSquare, Plus, Search, X } from 'lucide-react'
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { SessionEditModal } from '@/components/session/SessionEditModal'
import { SessionList } from '@/components/session/SessionList'
import { SessionSearch } from '@/components/session/SessionSearch'
import { Button } from '@/components/ui/button'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { cn } from '@/lib/utils'
import { reportError } from '@/services/errorReporting'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useAgentStore } from '@/stores/agentStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import type { Session } from '@/types'

interface SidebarProps {
  /** 是否为移动端 */
  isMobile?: boolean
}

/**
 * 侧边栏尺寸常量
 * 使用 CSS 变量定义的设计令牌
 *
 * 对应 design-tokens.css 中的变量:
 * - --sidebar-header-height: 40px
 * - --sidebar-padding: 8px
 * - --sidebar-item-height: 40px
 * - --sidebar-search-height: 28px
 * - --sidebar-width: 220px
 *
 * 响应式设计:
 * - 大屏幕 (>1280px): 220px
 * - 小桌面 (768px-1280px): 200px (更窄以节省空间)
 * - 移动端 (<768px): 280px (全宽遮罩)
 */
const SIDEBAR_STYLES = {
  // 头部高度 40px
  headerHeight: 'h-10', // 40px = 10 * 4px (Tailwind)
  // 内边距 8px
  padding: 'p-2', // 8px = 2 * 4px (Tailwind)
  paddingX: 'px-2',
  // 新建按钮尺寸 28px
  buttonSize: 'sm' as const,
  // 搜索框高度 28px
  searchHeight: 'h-7', // 28px = 7 * 4px (Tailwind)
  // 会话列表项高度 40px
  itemHeight: 40,
  // 侧边栏宽度
  width: {
    desktop: 220, // 大屏幕默认宽度
    smallDesktop: 200, // 小桌面宽度 (1280px以下)
    mobile: 280, // 移动端宽度
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
  // 模态框统一状态: { mode: 'create' } 或 { mode: 'edit', sessionId } 或 null
  const [modal, setModal] = useState<{ mode: 'create' | 'edit'; sessionId?: string } | null>(null)
  const [isSaving, setIsSaving] = useState(false)

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
  const searchSessions = useSessionListStore((state) => state.searchSessions)
  const fetchSessions = useSessionListStore((state) => state.fetchSessions)
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)
  const setSidebarCollapsed = useUIStore((state) => state.setSidebarCollapsed)
  const toggleSidebar = useUIStore((state) => state.toggleSidebar)
  const messageSearchQuery = useUIStore((state) => state.messageSearchQuery)
  const setMessageSearchQuery = useUIStore((state) => state.setMessageSearchQuery)

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

  // 根据搜索关键词过滤会话 - 使用 useMemo 缓存计算结果
  const filteredSessions = useMemo(
    () => (searchKeyword ? searchSessions(searchKeyword) : sessions),
    [searchKeyword, searchSessions, sessions],
  )

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
      // 直接导航到会话页面，而不是主页
      navigate(`/session/${sessionId}`)
    },
    [setActiveSession, navigate],
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
          // 新建会话
          const session = await createSession(title || undefined, {
            agentId: agentId || undefined,
          })
          setModal(null)
          navigate(`/session/${session.id}`)
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
    [createSession, renameSession, updateSessionAgent, navigate],
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
          'border-border/50 flex flex-col border-r transition-all duration-300 ease-in-out',
          // 侧边栏淡底色，与主对话区视觉分离
          'bg-[var(--sidebar-bg-light)] dark:bg-[var(--sidebar-bg-dark)]',
          // 移动端样式：固定定位，从左侧滑入
          isMobile && !sidebarCollapsed && 'fixed top-0 left-0 z-50 h-full shadow-2xl',
        )}
        style={
          sidebarCollapsed && !isMobile
            ? { width: '48px', minWidth: '48px', maxWidth: '48px', flexShrink: 0 }
            : isMobile
              ? { width: '280px', minWidth: '280px', maxWidth: '280px', flexShrink: 0 }
              : { width: '200px', minWidth: '200px', maxWidth: '220px', flexShrink: 0 }
        }
      >
        {/* ---- 折叠状态：仅显示图标栏（48px） ---- */}
        {sidebarCollapsed && !isMobile ? (
          <div className="flex h-full flex-col items-center py-3">
            {/* 展开按钮 */}
            <button
              onClick={toggleSidebar}
              className="hover:bg-accent text-muted-foreground hover:text-foreground mb-3 flex h-8 w-8 items-center justify-center rounded-md transition-colors"
              aria-label="展开侧边栏"
              title="展开侧边栏"
              data-testid="sidebar-expand-button"
            >
              <ChevronRight className="h-4 w-4" />
            </button>

            {/* 新建会话图标 */}
            <button
              onClick={handleOpenNewSessionModal}
              className="hover:bg-accent text-muted-foreground hover:text-foreground mb-3 flex h-8 w-8 items-center justify-center rounded-md transition-colors"
              aria-label="新建会话"
              title="新建会话"
            >
              <Plus className="h-4 w-4" />
            </button>

            {/* 搜索图标（点击展开） */}
            <button
              onClick={toggleSidebar}
              className="hover:bg-accent text-muted-foreground hover:text-foreground mb-3 flex h-8 w-8 items-center justify-center rounded-md transition-colors"
              aria-label="搜索会话"
              title="搜索会话"
            >
              <Search className="h-4 w-4" />
            </button>

            {/* 会话图标列表（最多显示 8 个） */}
            <div className="flex-1 overflow-y-auto">
              {sessions.slice(0, 8).map((session) => (
                <button
                  key={session.id}
                  onClick={() => handleSessionClick(session.id)}
                  className={cn(
                    'hover:bg-accent mb-1 flex h-8 w-8 items-center justify-center rounded-md transition-colors',
                    activeSessionId === session.id
                      ? 'bg-accent text-accent-foreground'
                      : 'text-muted-foreground',
                  )}
                  title={session.title}
                  aria-label={`切换到会话: ${session.title}`}
                >
                  <MessageSquare className="h-3.5 w-3.5" />
                </button>
              ))}
            </div>
          </div>
        ) : (
          /* ---- 展开状态：显示完整内容 ---- */
          <>
            {/* 侧边栏头部 - Requirements: 9.1, 9.2, 9.3 */}
            <div
              className={cn(
                'border-border flex items-center justify-between border-b',
                SIDEBAR_STYLES.headerHeight,
                SIDEBAR_STYLES.paddingX,
              )}
              data-testid="sidebar-header"
            >
              <h2 className="text-foreground text-base font-semibold">会话</h2>
              <div className="flex items-center gap-1">
                {/* 移动端关闭按钮 */}
                {isMobile && (
                  <Button
                    size={SIDEBAR_STYLES.buttonSize}
                    variant="ghost"
                    onClick={handleCloseSidebar}
                    aria-label="关闭侧边栏"
                    title="关闭侧边栏"
                    data-testid="close-sidebar-button"
                    className="h-7 w-7 p-0"
                  >
                    <X className="h-3.5 w-3.5" />
                  </Button>
                )}
                {/* 折叠按钮 */}
                {!isMobile && (
                  <button
                    onClick={toggleSidebar}
                    className="hover:bg-accent text-muted-foreground hover:text-foreground flex h-7 w-7 items-center justify-center rounded-md transition-colors"
                    aria-label="折叠侧边栏"
                    title="折叠侧边栏"
                    data-testid="sidebar-collapse-button"
                  >
                    <ChevronLeft className="h-3.5 w-3.5" />
                  </button>
                )}
                {/* 新建按钮 28px */}
                <Button
                  size={SIDEBAR_STYLES.buttonSize}
                  variant="default"
                  onClick={handleOpenNewSessionModal}
                  aria-label="新建会话"
                  title="新建会话"
                  data-testid="new-session-button"
                  className="h-7 w-7 p-0"
                >
                  <Plus className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>

            {/* 搜索框区域 - Requirements: 9.3, 9.5 */}
            <div
              className={cn('border-border/50 overflow-hidden border-b', SIDEBAR_STYLES.padding)}
              data-testid="sidebar-search-section"
            >
              <SessionSearch
                onSearchChange={setSearchKeyword}
                resultCount={filteredSessions.length}
                totalCount={sessions.length}
                className="sidebar-search"
                inputClassName={SIDEBAR_STYLES.searchHeight}
              />
              {/* 消息搜索框 */}
              <div className="relative mt-1">
                <Search className="text-muted-foreground absolute top-1/2 left-2 h-3.5 w-3.5 -translate-y-1/2" />
                <input
                  type="text"
                  placeholder="搜索消息内容..."
                  value={messageSearchQuery}
                  onChange={(e) => setMessageSearchQuery(e.target.value)}
                  className={cn(
                    'bg-muted/50 border-border/50 focus:border-primary w-full rounded-md border py-1 pr-7 pl-7 text-xs outline-none transition-colors',
                    SIDEBAR_STYLES.searchHeight,
                  )}
                  aria-label="搜索消息内容"
                />
                {messageSearchQuery && (
                  <button
                    onClick={() => setMessageSearchQuery('')}
                    className="text-muted-foreground hover:text-foreground absolute top-1/2 right-1.5 -translate-y-1/2 rounded p-0.5"
                    aria-label="清除消息搜索"
                  >
                    <X className="h-3 w-3" />
                  </button>
                )}
              </div>
            </div>

            {/* 会话列表 - Requirements: 9.3, 9.4 */}
            <div className="scrollbar-thin min-h-0 flex-1 overflow-x-hidden overflow-y-auto">
              {isLoading ? (
                <div
                  className={cn(
                    'flex flex-col items-center justify-center text-center',
                    SIDEBAR_STYLES.padding,
                    'py-8',
                  )}
                >
                  <Loader2 className="text-muted-foreground mb-2 h-6 w-6 animate-spin" />
                  <p className="text-muted-foreground text-sm">加载中...</p>
                </div>
              ) : filteredSessions.length === 0 ? (
                <div
                  className={cn(
                    'flex flex-col items-center justify-center text-center',
                    SIDEBAR_STYLES.padding,
                    'py-8',
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
                  className="px-2"
                  itemHeight={SIDEBAR_STYLES.itemHeight}
                />
              )}
            </div>
          </>
        )}
      </aside>

      {/* 模态框放在 aside 外面，折叠/展开状态下都能渲染 */}
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
