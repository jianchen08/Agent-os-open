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

import { memo, useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ChatIcon, ChatActiveIcon, Loader2, Plus, X } from '@/assets/icons'
import { ChangePasswordForm } from '@/components/auth/ChangePasswordForm'
import { LoginModal } from '@/components/auth/LoginModal'
import { NotificationCenter } from '@/components/chat/NotificationCenter'
import { PluginStatusItems } from '@/components/layout/StatusItems'
import { ThemeButton } from '@/components/layout/ThemeButton'
import { SessionEditModal, type SessionFormOptions } from '@/components/session/SessionEditModal'
import { SessionList } from '@/components/session/SessionList'
import { SessionSearch } from '@/components/session/SessionSearch'
import { SlotHost } from '@/components/slots/SlotHost'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { useSessionsQuery } from '@/hooks/queries/useSessionsQuery'
import { cn } from '@/lib/utils'
import { searchGlobal, type SessionSearchHit, type MessageSearchHit } from '@/services/api/search'
import { performLogout } from '@/services/auth/logout'
import { reportError, ErrorSeverity, ErrorType } from '@/services/errorReporting'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { evaluateWhen } from '@/services/schema/whenExpression'
import { registerSessionProject, createSessionWithProject } from '@/services/sessionCreation'
import { saveSessionExecutionOptions } from '@/services/sessionExecutionOptions'
import { subscribeHighlight } from '@/services/uiBridge/commandExecutor'
import { openPluginPage, openWorkspacePanel, openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import {
  resolveSessionByPipeline,
  SettingsEntryButton,
  SIDEBAR_STYLES,
} from './SidebarParts'
import { SidebarBellButton, SidebarUserMenu } from './SidebarUserMenu'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import type { Session } from '@/types'


interface SidebarProps {
  /** 是否为移动端 */
  isMobile?: boolean
}

/**
 * 侧边栏组件
 * 使用 memo 和 useMemo 优化性能
 *
 * Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.3
 * - 新增: 创建会话后自动导航到会话页面
 * - 新增: 移动端响应式支持，带遮罩层和关闭按钮
 */
export const Sidebar = memo<SidebarProps>(({ isMobile = false }) => {
  const [searchKeyword, setSearchKeyword] = useState('')
  /** 后端搜索结果（防抖调用 monitoring 插件 search） */
  const [searchResults, setSearchResults] = useState<{
    sessions: SessionSearchHit[]
    messages: MessageSearchHit[]
  }>({ sessions: [], messages: [] })
  const [isSearching, setIsSearching] = useState(false)
  const [searchError, setSearchError] = useState(false)
  // 模态框统一状态: { mode: 'create' } 或 { mode: 'edit', sessionId } 或 null
  const [modal, setModal] = useState<{ mode: 'create' | 'edit'; sessionId?: string } | null>(null)
  const [isSaving, setIsSaving] = useState(false)
  const activeView = useUIStore((s) => s.sidebarActiveView)
  const setActiveView = useUIStore((s) => s.setSidebarActiveView)
  // uiBridge 命令面高亮态（P1 高亮最小增量：订阅 ui_command highlight 广播，≤5s 自动消退）
  const [highlightItemId, setHighlightItemId] = useState<string | null>(null)
  useEffect(() => subscribeHighlight((hl) => setHighlightItemId(hl === null ? null : hl.itemId)), [])
  const [contribTick, setContribTick] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => {
      // 侧栏入口 = workspace 空间 + activity-bar 栏位（旧 viewsContainers 归一化产物）
      const n = contributionRegistry
        .getPagesBySpace('workspace')
        .filter((p) => p.slot === 'activity-bar').length
      setContribTick((prev) => (prev === n ? prev : n))
    }, 1500)
    return () => window.clearInterval(id)
  }, [])
  const user = useAuthStore((s) => s.user)
  const pluginContainers = useMemo(() => {
    void contribTick
    return (
      contributionRegistry
        .getPagesBySpace('workspace')
        .filter((p) => p.slot === 'activity-bar')
        // when 条件（ADR §3.4）：声明了 when 的插件页面按 context keys 求值，
        // 不满足则不在侧边栏显示
        .filter((p) =>
          p.when ? evaluateWhen(p.when, { 'user.role': user?.role ?? 'guest' }) : true,
        )
        .slice()
        .sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
    )
  }, [contribTick, user])
  const isAuthenticated = useAuthStore((s) => s.isAuthenticated)
  const navigate = useNavigate()
  /** 登出进行中（菜单项禁用 +「退出中…」，防重复点击；成功后页面跳转卸载，
   *  失败通知落在操作点并复位可重试） */
  const [loggingOut, setLoggingOut] = useState(false)
  /** 登出走统一编排（WS/流式清理+logout+跳转），与 router.tsx 的 onLogout 同源 */
  const handleLogout = useCallback(() => {
    setLoggingOut(true)
    performLogout(navigate)
      .catch((error: unknown) => {
        // 失败可见（不静默装成功）：通知 + 菜单项复位（finally）可就地重试
        useNotificationStore.getState().addNotification({
          title: '退出登录失败',
          message: (error as { message?: string })?.message ?? '请稍后重试',
          priority: 'normal',
          category: 'error',
          isBlocking: false,
          autoDismissMs: 5000,
          sourceLabel: '前端',
        })
      })
      .finally(() => setLoggingOut(false))
  }, [navigate])

  // 弹窗式登录框状态（点菜单里的"登录"才打开）
  const [loginModalOpen, setLoginModalOpen] = useState(false)
  const [changePasswordOpen, setChangePasswordOpen] = useState(false)

  // 会话列表（query 化）：缓存秒开 + 后台静默刷新；仅在无缓存冷加载时显示 loading
  const { data: sessions = [], isPending: isSessionsPending } = useSessionsQuery()
  const activeSessionId = useSessionStore((state) => state.activeSessionId)
  const deletingSessionIds = useSessionStore((state) => state.deletingSessionIds)
  const isLoading = isSessionsPending && sessions.length === 0
  const setActiveSession = useSessionListStore((state) => state.setActiveSession)
  const deleteSession = useSessionListStore((state) => state.deleteSession)
  const copySession = useSessionListStore((state) => state.copySession)
  const toggleSessionStar = useSessionListStore((state) => state.toggleSessionStar)
  const toggleSessionPin = useSessionListStore((state) => state.toggleSessionPin)
  const renameSession = useSessionListStore((state) => state.renameSession)
  const updateSessionAgent = useSessionListStore((state) => state.updateSessionAgent)
  const updateSessionWorkspace = useSessionListStore((state) => state.updateSessionWorkspace)
  const sidebarCollapsed = useUIStore((state) => state.sidebarCollapsed)
  const setSidebarCollapsed = useUIStore((state) => state.setSidebarCollapsed)
  const setMessageJump = useUIStore((state) => state.setMessageJump)
  // P5：通知唯一入口——底栏 Bell 绑定 notificationStore（与 NotificationCenter 同一 store）
  const toggleNotificationPanel = useNotificationStore((s) => s.togglePanel)
  const notificationUnreadCount = useNotificationStore(
    (s) => s.notifications.filter((n) => !n.isRead).length,
  )


  /**
   * 统一搜索：防抖调用后端搜索 API（端点=monitoring 插件 plugin.json
   * http_endpoints 声明投影 mon_search_global——真值源在声明面，改名经
   * endpoints.generated.ts 重生成后 tsc 闸红跟随，壳层无路径词面）。
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
   * 移动端会话面收口：点会话/消息命中/新建完成 = 用户要回对话主管道视图，
   * 抽屉与工作区覆盖层一并收起（真机缺陷"视图切换失灵"收口缺员：窄窗形态下
   * 工作区覆盖层盖住整个主区，会话切换在其背后发生，用户视角"钉死在文件/面板
   * 内容视图"；与 M4/M5/M8 抽屉收口同族，收口单点在此）。
   */
  const closeMobileWorkspaceSurfaces = useCallback(() => {
    setSidebarCollapsed(true)
    useUIStore.getState().setMobileWorkspaceOpen(false)
  }, [setSidebarCollapsed])

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
   * 处理消息搜索结果点击：解析管道归属会话 → 切会话/子 Tab → 记录定位目标。
   *
   * 定位目标写入 uiStore（pipelineId + sequence），由 ChatContainer 在目标管道
   * 激活后透传给 MessageList 滚动定位并消费清除——跨会话/子管道切换的中间态
   * 不会丢失跳转意图，也不会误定位到错误管道。
   */
  const handleMessageHitClick = useCallback(
    async (hit: MessageSearchHit) => {
      const session = resolveSessionByPipeline(hit.session_id, sessions)
      if (!session) return
      // sequence 缺失（旧数据/异常行）时无定位键，仅切会话不定位
      if (typeof hit.sequence === 'number') {
        setMessageJump({ pipelineId: hit.session_id, sequence: hit.sequence })
      }
      const tabStore = useAgentTabStore.getState()
      const isSameSession = useSessionStore.getState().activeSessionId === session.id
      if (!isSameSession) {
        await handleSessionClick(session.id)
      }
      // 命中子管道且存在对应 Tab → 切到该 Tab（同一会话内直接切；跨会话在
      // initSessionTabs 恢复 Tab 后切）。Tab 不存在（历史子管道从未在客户端
      // 建档）时停留在主 Tab，跳转目标等管道激活后消费。
      const tabId = tabStore.getTabIdByPipeline(hit.session_id)
      if (tabId && tabId !== tabStore.activeTabId) {
        tabStore.switchToTab(tabId)
      }
      if (isMobile) closeMobileWorkspaceSurfaces()
    },
    [sessions, handleSessionClick, setMessageJump, isMobile],
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
   * 会话目录 → 项目登记迁移至 services/sessionCreation（与声明式表单页共享）。
   */

  /**
   * 确认创建 / 编辑会话
   */
  const handleSaveSession = useCallback(
    async (
      sessionId: string | null,
      title: string,
      agentId: string | null,
      options?: SessionFormOptions,
    ) => {
      setIsSaving(true)
      try {
        if (sessionId) {
          // 编辑已有会话 — 标题/Agent 走服务端；插件表单值保存为本地整包
          // 快照（values + 已组装的 executionContext），下一次发消息以消息级
          // execution_context 生效（执行字段本质是主管道 state 的执行上下文
          // 声明，服务端 PATCH 会话不承载）。
          await renameSession(sessionId, title)
          await updateSessionAgent(sessionId, agentId)
          // 工作空间字段改动写回服务端 metadata（会话树聚合/列表徽标真值源）：
          // 只存本地执行快照的话 session.workspace 不变，聚合不随编辑走。
          const nextWorkspace = options?.fieldMetadata?.workspace
          if (typeof nextWorkspace === 'string' && nextWorkspace.trim()) {
            const nextMode = options?.fieldMetadata?.workspace_mode
            await updateSessionWorkspace(
              sessionId,
              nextWorkspace.trim(),
              nextMode === 'plain' || nextMode === 'worktree' ? nextMode : undefined,
            )
          }
          saveSessionExecutionOptions(sessionId, {
            values: options?.fieldMetadata ?? {},
            ...(options?.executionContext
              ? { executionContext: options.executionContext }
              : {}),
          })
          await registerSessionProject(sessionId, title, options)
          setModal(null)
        } else {
          // 新建会话：createSession 内部已设置 activeSessionId，
          // ChatContainer 会随 activeSessionId 自动渲染，无需 navigate。
          await createSessionWithProject(title || undefined, agentId, options)
          setModal(null)
        }
        // 新建/编辑成功即收抽屉：新会话聊天在抽屉背后，不收则用户看到的
        // 仍是列表（与 M4/M5 opener 收口同族，此为弹窗直调链路的最后成员）
        if (isMobile) closeMobileWorkspaceSurfaces()
      } catch (error) {
        reportError(error instanceof Error ? error.message : String(error), {
          type: ErrorType.SERVER,
          severity: ErrorSeverity.ERROR,
          componentName: 'Sidebar',
          operation: sessionId ? 'saveSessionEdit' : 'createSession',
          sessionId: sessionId || undefined,
        })
      } finally {
        setIsSaving(false)
      }
    },
    [isMobile, closeMobileWorkspaceSurfaces, renameSession, updateSessionAgent, updateSessionWorkspace],
  )

  /**
   * 会话树顶栏一键新建：以该工作空间为工作空间直接创建会话（不开弹窗）。
   * 与弹窗创建同链路（createSessionWithProject：metadata 出生落库 + 项目登记）；
   * 拓扑取 plain 默认——worktree 属显式选择项，仅弹窗内可表。
   */
  const handleCreateInWorkspace = useCallback(
    async (workspace: string) => {
      try {
        await createSessionWithProject(undefined, null, {
          fieldMetadata: { workspace, workspace_mode: 'plain' },
          executionContext: { workspace: { source_path: workspace, mode: 'plain' } },
        })
      } catch (error) {
        reportError(error instanceof Error ? error.message : String(error), {
          type: ErrorType.SERVER,
          severity: ErrorSeverity.ERROR,
          componentName: 'Sidebar',
          operation: 'createSessionInWorkspace',
        })
      }
    },
    [],
  )

  /**
   * 处理编辑会话 - 打开编辑模态框
   */
  const handleEditSession = useCallback((session: Session) => {
    setModal({ mode: 'edit', sessionId: session.id })
  }, [])

  /** 复制会话进行中的会话 ID 集合（行级 Loader2 pending 真值源） */
  const [copyingSessionIds, setCopyingSessionIds] = useState<ReadonlySet<string>>(new Set())
  /**
   * 处理复制会话（await 期间行内转圈；失败通知落在操作点）
   */
  const handleCopySession = useCallback(
    async (session: Session) => {
      setCopyingSessionIds((prev) => new Set(prev).add(session.id))
      try {
        await copySession(session.id)
      } catch (error) {
        reportError(error instanceof Error ? error.message : String(error), {
          type: ErrorType.SERVER,
          severity: ErrorSeverity.ERROR,
          componentName: 'Sidebar',
          operation: 'copySession',
          sessionId: session.id,
        })
        useNotificationStore.getState().addNotification({
          title: '复制会话失败',
          message:
            (error as { message?: string })?.message ?? '请检查网络连接后重试',
          priority: 'normal',
          category: 'error',
          isBlocking: false,
          autoDismissMs: 5000,
          sourceLabel: '前端',
        })
      } finally {
        setCopyingSessionIds((prev) => {
          const next = new Set(prev)
          next.delete(session.id)
          return next
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
  const editingSession =
    modal?.mode === 'edit' && modal.sessionId
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
   * 处理会话点击（移动端自动收抽屉+工作区覆盖层，回对话主管道视图）
   */
  const handleSessionClickMobile = useCallback(
    async (sessionId: string) => {
      await handleSessionClick(sessionId)
      if (isMobile) {
        closeMobileWorkspaceSurfaces()
      }
    },
    [handleSessionClick, isMobile, closeMobileWorkspaceSurfaces],
  )

  const handlePluginClick = useCallback(
    (entry: PageDeclaration) => {
      setActiveView(entry.id)
      if (entry.path && typeof entry.path === 'string') {
        // 声明页一律经 opener 解析；解析失败由 opener 显式报错，不做 navigate
        // 兜底（未声明路径落 '*' 通配只会静默回首页，P0-1）
        openWorkspacePanelByPath(entry.path)
      } else if (entry.widget) {
        // workspace.tab 域走 SlotRegistry keyed 槽（C4 收敛）；非收敛域
        // widget 直开（opener pluginPageSpec 统一映射）
        openPluginPage(entry)
      } else {
        // 容器条目（viewsContainers）：优先取容器下声明了 widget 的视图条目，
        // 带其静态 props 渲染（声明数据直通 widget）；无视图条目时用容器 id 兜底
        const viewEntry = contributionRegistry.getViews(entry.id).find((v) => v.widget)
        openWorkspacePanel({
          id: `ws-plugin-${viewEntry?.id ?? entry.id}`,
          title: viewEntry?.title || entry.title || entry.id,
          component: viewEntry?.widget || entry.id,
          icon: viewEntry?.icon || entry.icon,
          props: viewEntry?.props,
          moduleId: entry.pluginId ? `__plugin_${entry.pluginId}__` : `__contrib_${entry.id}__`,
        })
      }
      if (isMobile) setSidebarCollapsed(true)
    },
    [isMobile, setSidebarCollapsed],
  )

  const handleSessionsClick = useCallback(() => {
    setActiveView('sessions')
  }, [])

  /**
   * 任务管理常驻入口：任务管理页归前端自有（用户裁定 2026-09-21，
   * TOP_NAV_PANELS['/tasks']），不经插件 contributes.pages 声明，
   * 故与插件条目（activity-bar 槽位）并排静态渲染并共用高亮态。
   */
  const handleTasksClick = useCallback(() => {
    setActiveView('tasks')
    openWorkspacePanelByPath('/tasks')
    if (isMobile) setSidebarCollapsed(true)
  }, [isMobile, setSidebarCollapsed])

  return (
    <>
      {/* 移动端遮罩层 */}
      {isMobile && !sidebarCollapsed && (
        <div
          data-testid="sidebar-overlay"
          className="animate-in fade-in fixed inset-0 z-40 bg-[var(--overlay-bg)] duration-200"
          onClick={handleCloseSidebar}
          aria-hidden="true"
        />
      )}

      <aside
        data-testid="sidebar"
        className={cn(
          // 必须 h-full：父级是 Splitter 内 full-height panel；否则 flex-1 无效，底部空白
          'border-border/50 flex h-full min-h-0 flex-col border-r transition-all duration-300 ease-in-out',
          // 主题侧边栏背景（--sidebar-bg 由主题引擎输出，自带明暗，无需 dark: 变体；
          // 区域背景关系 --region-sidebar-bg 由 .theme-sidebar-area CSS 规则统一消费）
          // theme-sidebar-area 叠加主题区域纹理（--sidebar-texture）
          'theme-sidebar-area',
          // 移动端 fixed 抽屉：顶沿避让状态栏（全屏沉浸下系统栏浮于内容上，
          // --safe-area-top 由壳注入/env 提供，桌面=0）；fixed 相对视口定位，
          // 不受外层抽屉容器 top 约束。底部 = safe-area-bottom 锚定 + 盒内
          // safe-area-pb（P0-6：高度由 top/bottom 双锚定拉伸求出，不靠
          // 100dvh calc——top 偏移与 dvh 叠加的时序差会把底栏推出屏外，
          // MuMu 实测 shots/03；safe-area-pb 再兜 env 通道）
          isMobile
          && !sidebarCollapsed
          && 'fixed left-0 bottom-[var(--safe-area-bottom,0px)] z-50 shadow-2xl top-[var(--safe-area-top,0px)] safe-area-pb',
        )}
        style={
          sidebarCollapsed && !isMobile
            ? {
                width: 0,
                minWidth: 0,
                maxWidth: 0,
                flexShrink: 0,
                overflow: 'hidden',
                border: 'none',
                padding: 0,
              }
            : isMobile
              ? {
                  width: `${SIDEBAR_STYLES.width.mobile}px`,
                  minWidth: `${SIDEBAR_STYLES.width.mobile}px`,
                  maxWidth: `${SIDEBAR_STYLES.width.mobile}px`,
                  flexShrink: 0,
                  // 高度交给 top/bottom 双锚定拉伸（className 的 bottom-[var(--safe-area-bottom)]
                  // + top-[var(--safe-area-top)]），inline height:auto 压掉基类 h-full——
                  // 100dvh calc 与 top 偏移叠加在 insets 动态时序下会把底栏推出屏外
                  // （P0-6，MuMu 实测 shots/03，2026-10-13 的 dvh 减法修复实测无效）
                  height: 'auto',
                }
              : {
                  width: '100%',
                  height: '100%',
                  minWidth: 0,
                  maxWidth: '100%',
                  minHeight: 0,
                  flexShrink: 0,
                  backgroundColor:
                    'var(--region-sidebar-bg, var(--ds-bg-panel, var(--sidebar-bg, hsl(var(--card)))))',
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
                'mb-2 flex h-10 w-10 items-center justify-center rounded-lg transition-colors',
                activeView === 'sessions'
                  ? 'text-[var(--ds-accent-primary)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
              )}
              style={
                activeView === 'sessions'
                  ? {
                      background: 'var(--ds-bg-elevated)',
                      boxShadow: 'inset 0 0 0 1px var(--ds-border-active)',
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
            {/* 任务管理常驻入口（前端自有页，与插件条目同形态） */}
            <button
              type="button"
              onClick={handleTasksClick}
              className={cn(
                'mb-2 flex h-10 w-10 items-center justify-center rounded-lg transition-colors',
                activeView === 'tasks'
                  ? 'text-[var(--ds-accent-primary)]'
                  : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
                highlightItemId === 'builtin.tasks' && 'ring-1 ring-[var(--ds-accent-primary)]',
              )}
              style={
                activeView === 'tasks'
                  ? {
                      background: 'var(--ds-bg-elevated)',
                      boxShadow: 'inset 0 0 0 1px var(--ds-border-active)',
                    }
                  : undefined
              }
              title="任务管理"
              aria-label="任务管理"
              data-testid="sidebar-rail-tasks"
            >
              <span className="text-[14px]">📋</span>
            </button>
            {pluginContainers.map((entry) => {
              const selected = activeView === entry.id
              return (
                <button
                  key={entry.id}
                  type="button"
                  onClick={() => handlePluginClick(entry)}
                  className={cn(
                    'mb-2 flex h-10 w-10 items-center justify-center rounded-lg transition-colors',
                    selected
                      ? 'text-[var(--ds-accent-primary)]'
                      : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
                    highlightItemId === entry.id && 'ring-1 ring-[var(--ds-accent-primary)]',
                  )}
                  style={
                    selected
                      ? {
                          background: 'var(--ds-bg-elevated)',
                          boxShadow:
                            'inset 0 0 0 1px var(--ds-border-active)',
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
            <div
              className="mt-auto flex flex-col items-center gap-2 pb-1"
              data-testid="sidebar-rail-footer"
            >
              <SidebarUserMenu
                variant="rail"
                isAuthenticated={isAuthenticated}
                user={user as { username?: string; email?: string } | null}
                loggingOut={loggingOut}
                onOpenLogin={() => setLoginModalOpen(true)}
                onOpenChangePassword={() => setChangePasswordOpen(true)}
                onLogout={handleLogout}
              />
              <SettingsEntryButton rail />
              <ThemeButton compact />
              <SidebarBellButton
                variant="rail"
                unreadCount={notificationUnreadCount}
                onClick={toggleNotificationPanel}
              />
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
                  className={cn(
                    'text-foreground mb-1 flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] font-medium transition-colors hover:bg-[var(--hover-overlay)]',
                    highlightItemId === 'builtin.new_session' && 'ring-1 ring-[var(--ds-accent-primary)]',
                  )}
                  data-testid="new-session-button"
                >
                  <span
                    className="flex h-6 w-6 items-center justify-center rounded-md"
                    style={{ background: 'var(--ds-bg-elevated)' }}
                  >
                    <Plus className="h-3.5 w-3.5 text-[var(--ds-accent-primary)]" />
                  </span>
                  新建会话
                </button>

                {/* 任务管理常驻入口（前端自有页，用户裁定 2026-09-21：不入插件声明） */}
                <button
                  type="button"
                  onClick={handleTasksClick}
                  className={cn(
                    'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors',
                    activeView === 'tasks'
                      ? 'text-foreground'
                      : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
                    highlightItemId === 'builtin.tasks' && 'ring-1 ring-[var(--ds-accent-primary)]',
                  )}
                  style={
                    activeView === 'tasks'
                      ? {
                          background: 'var(--ds-bg-elevated)',
                          boxShadow:
                            'inset 0 0 0 1px var(--ds-border-active)',
                        }
                      : undefined
                  }
                  data-testid="sidebar-menu-tasks"
                  title="任务管理"
                >
                  <span className="flex h-4 w-4 shrink-0 items-center justify-center text-[13px] opacity-90">
                    📋
                  </span>
                  <span className="min-w-0 flex-1 truncate">任务管理</span>
                </button>

                {/* plugin contributed pages — workspace/activity-bar（vscode-like） */}
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
                          : 'text-muted-foreground hover:text-foreground hover:bg-[var(--hover-overlay)]',
                        highlightItemId === entry.id && 'ring-1 ring-[var(--ds-accent-primary)]',
                      )}
                      style={
                        selected
                          ? {
                              background: 'var(--ds-bg-elevated)',
                              boxShadow:
                                'inset 0 0 0 1px var(--ds-border-active)',
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
                        onClick={() => handleMessageHitClick(hit)}
                        className="text-muted-foreground hover:text-foreground block w-full truncate rounded px-2 py-1 text-left text-[11px] transition-colors hover:bg-[var(--hover-overlay)]"
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

              {/* 会话列表 - Requirements: 9.3, 9.4（滚动由 SessionList 自持容器承担） */}
              <div className="min-h-0 flex-1">
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
                    copyingSessionIds={copyingSessionIds}
                    onSessionClick={handleSessionClickMobile}
                    onDeleteSession={deleteSession}
                    onEditSession={handleEditSession}
                    onCopySession={handleCopySession}
                    onStarSession={handleStarSession}
                    onPinSession={handlePinSession}
                    onResetMessages={handleResetMessages}
                    onCreateInWorkspace={handleCreateInWorkspace}
                    className="px-2"
                    itemHeight={SIDEBAR_STYLES.itemHeight}
                  />
                )}
              </div>

              {/* sidebar.block 槽锚点（C4 槽位机制，会话列表下方区块；
                  布局契约=全宽纵向卡流带上限，条目渲染进宿主容器——
                  设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4） */}
              <SlotHost
                slotKey="sidebar.block"
                whenContext={{ 'user.role': user?.role ?? 'guest' }}
                className="max-h-64 shrink-0 overflow-y-auto px-2 pb-1"
              />

              {/* 底栏：紧凑单行，不占大空白 */}
              {/* 插件状态项条带（dock/status 贡献，原 StatusBar 迁移；无项不渲染） */}
              <PluginStatusItems />
              <div
                className="border-border mt-auto flex min-h-9 shrink-0 items-center gap-0.5 border-t px-1.5 pb-[max(env(safe-area-inset-bottom,0px),var(--safe-inset-bottom,0px),8px)]"
                data-testid="sidebar-footer"
              >
                <SidebarUserMenu
                  variant="footer"
                  isAuthenticated={isAuthenticated}
                  user={user as { username?: string; email?: string } | null}
                  loggingOut={loggingOut}
                  onOpenLogin={() => setLoginModalOpen(true)}
                  onOpenChangePassword={() => setChangePasswordOpen(true)}
                  onLogout={handleLogout}
                />
                <SettingsEntryButton />
                <ThemeButton compact />
                <SidebarBellButton
                  variant="footer"
                  unreadCount={notificationUnreadCount}
                  onClick={toggleNotificationPanel}
                />
              </div>
            </div>
          </>
        )}
      </aside>
      {/* P5: 通知中心唯一入口承载——侧边栏 Bell 调用 togglePanel，面板/阻塞模态框在此挂载 */}
      <NotificationCenter hideTrigger />

      {/* 弹窗式登录框：点击底栏用户区域即弹出（不跳转页面） */}
      <LoginModal open={loginModalOpen} onClose={() => setLoginModalOpen(false)} />

      {/* 账户设置·修改口令（D1-3）：成功后自动关闭 */}
      <Dialog open={changePasswordOpen} onOpenChange={setChangePasswordOpen}>
        <DialogContent className="sm:max-w-md" data-testid="change-password-dialog">
          <DialogHeader>
            <DialogTitle>修改密码</DialogTitle>
          </DialogHeader>
          <ChangePasswordForm onSuccess={() => setChangePasswordOpen(false)} />
        </DialogContent>
      </Dialog>

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
