/**
 * 路由配置
 *
 * 定义应用的所有路由，包含登录/注册和受保护的主页面。
 * 主页包含完整的聊天界面：左侧会话列表 + 右侧聊天区域。
 */

import { MoreHorizontal, Pencil, Copy, Star, Pin, Trash2 } from 'lucide-react'
import { lazy, Suspense, useEffect, useState, useCallback, useMemo } from 'react'
import { createBrowserRouter, Navigate, useNavigate, useLocation } from 'react-router-dom'
import { ChatContainer } from './components/chat/ChatContainer'
import { AppHeader } from './components/layout/AppHeader'
import { FiveSpaceLayout } from './components/layout/FiveSpaceLayout'
import { Button } from './components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './components/ui/dropdown-menu'
import { ROUTES } from './constants/routes'
import { useConnectionStatus } from './hooks/useConnectionStatus'
import { useRealtimeEvents } from './hooks/useRealtimeEvents'
import { useTaskPolling } from './hooks/useTaskPolling'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { globalWS } from './services/websocket/GlobalWebSocket'
import { initStreamingEvents, destroyStreamingEvents } from './services/websocket/streamingEventService'
import { clearChunkTimeout } from './services/websocket/streaming/chunkTimeout'
import { flushStreamChunkBuffer } from './services/websocket/streaming/handlers/streamHandler'
import { useAgentTabStore } from './stores/agentTabStore'
import { useAuthStore } from './stores/authStore'
import { useInteractionStore } from './stores/interactionStore'
import { useLayoutModeStore } from './stores/layoutModeStore'
import { usePipelineMessageStore } from './stores/pipelineMessageStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useSessionStore } from './stores/sessionStore'
import { useStreamingStore } from './stores/streamingStore'
import { useUIStore } from './stores/uiStore'
import { generateUUID } from './utils/uuid'
import type { SendMessageParams } from './components/chat/types'
import type { Message } from './types/models'
import type { ReactNode } from 'react'

const ModulesSettingsPage = lazy(() =>
  import('@/pages/settings/ModulesSettingsPage').then((m) => ({ default: m.ModulesSettingsPage })),
)
const SettingsPage = lazy(() =>
  import('@/pages/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })),
)
const ApiSettingsPage = lazy(() =>
  import('@/pages/settings/ApiSettingsPage').then((m) => ({ default: m.ApiSettingsPage })),
)
const LlmSettingsPage = lazy(() =>
  import('@/pages/settings/LlmSettingsPage').then((m) => ({ default: m.LlmSettingsPage })),
)
const ContextWindowSettingsPage = lazy(() =>
  import('@/pages/settings/ContextWindowSettingsPage').then((m) => ({
    default: m.ContextWindowSettingsPage,
  })),
)
const ConcurrencySettingsPage = lazy(() =>
  import('@/pages/settings/ConcurrencySettingsPage').then((m) => ({
    default: m.ConcurrencySettingsPage,
  })),
)
const CostSettingsPage = lazy(() =>
  import('@/pages/settings/CostSettingsPage').then((m) => ({ default: m.CostSettingsPage })),
)
const ToolsPage = lazy(() =>
  import('@/pages/tools/ToolsPage').then((m) => ({ default: m.ToolsPage })),
)
const AgentsPage = lazy(() =>
  import('@/pages/agents/AgentsPage').then((m) => ({ default: m.AgentsPage })),
)
const MonitoringPage = lazy(() =>
  import('@/pages/monitoring/MonitoringPage').then((m) => ({ default: m.MonitoringPage })),
)
const AdminPage = lazy(() =>
  import('@/pages/admin/AdminPage').then((m) => ({ default: m.AdminPage })),
)
const MemoryPage = lazy(() =>
  import('@/pages/memory/MemoryPage').then((m) => ({ default: m.MemoryPage })),
)
const DebugPage = lazy(() =>
  import('@/pages/debug/DebugPage').then((m) => ({ default: m.DebugPage })),
)
const DebugExecutionRecordsPage = lazy(() =>
  import('@/pages/debug/DebugExecutionRecordsPage').then((m) => ({
    default: m.DebugExecutionRecordsPage,
  })),
)
const DebugSessionsPage = lazy(() =>
  import('@/pages/debug/DebugSessionsPage').then((m) => ({ default: m.DebugSessionsPage })),
)
const DebugTasksPage = lazy(() =>
  import('@/pages/debug/DebugTasksPage').then((m) => ({ default: m.DebugTasksPage })),
)
const DebugEvaluationMetricsPage = lazy(() =>
  import('@/pages/debug/DebugEvaluationMetricsPage').then((m) => ({
    default: m.DebugEvaluationMetricsPage,
  })),
)
const DebugUsersPage = lazy(() =>
  import('@/pages/debug/DebugUsersPage').then((m) => ({ default: m.DebugUsersPage })),
)
const PluginsSettingsPage = lazy(() =>
  import('@/pages/settings/PluginsSettingsPage').then((m) => ({
    default: m.PluginsSettingsPage,
  })),
)
const TriggersPage = lazy(() =>
  import('@/pages/triggers/TriggersPage').then((m) => ({ default: m.TriggersPage })),
)
const KnowledgeBasePage = lazy(() =>
  import('@/pages/knowledge-base/KnowledgeBasePage').then((m) => ({
    default: m.KnowledgeBasePage,
  })),
)

/** 懒加载 fallback */
const LazyFallback = <div className="text-muted-foreground p-4">加载中...</div>

/** 判断当前视口是否为移动端（< md 断点 768px） */
function isMobileViewport(): boolean {
  return typeof window !== 'undefined' && window.innerWidth < 768
}

// ============================================
// 路由守卫
// ============================================

/**
 * 路由守卫组件
 *
 * 检查用户认证状态：
 * - 正在初始化时显示加载动画
 * - 未认证时重定向到登录页
 * - 已认证时渲染子组件
 */
function ProtectedRoute({ children }: { children: ReactNode }): ReactNode {
  const { isAuthenticated, isInitializing } = useAuthStore()

  if (isInitializing) {
    return (
      <div className="bg-background text-foreground flex min-h-screen items-center justify-center">
        <div className="space-y-2 text-center">
          <div className="border-primary mx-auto h-8 w-8 animate-spin rounded-full border-2 border-t-transparent" />
          <p className="text-muted-foreground text-sm">加载中...</p>
        </div>
      </div>
    )
  }

  if (!isAuthenticated) {
    return <Navigate to={ROUTES.LOGIN} replace />
  }

  return children
}

// ============================================
// 聊天主页
// ============================================

/**
 * 聊天主页组件
 *
 * 登录后的主界面，包含：
 * - 顶部导航栏：应用标题、WebSocket 连接状态、用户信息、登出按钮
 * - 左侧面板：会话列表、新建会话按钮
 * - 右侧区域：聊天容器（ChatContainer）或欢迎页
 *
 * 核心流程：
 * 1. 组件挂载时获取会话列表
 * 2. 订阅 WebSocket 流式事件（stream_start/chunk/end、new_message）
 * 3. 用户选择或创建会话后，自动连接 WebSocket
 * 4. 用户发送消息通过 WebSocket 传输，流式响应实时更新
 */
function HomePage(): ReactNode {
  const navigate = useNavigate()
  const location = useLocation()
  const [showThemePanel, setShowThemePanel] = useState(false)
  const { user, logout } = useAuthStore()

  // Phase 1 hooks: connection status and real-time events
  useConnectionStatus()
  useRealtimeEvents()

  // 轮询长期任务状态，作为 WebSocket 断连时的 fallback
  useTaskPolling()

  // Layout mode toggle
  // BUG-FIX-fix_20260513_workspace_two_clicks:
  // 问题根因: toggleMode 只切换 mode 字段，不同步 workspaceCollapsed，
  //          导致切换到 five-space 模式后工作区仍为折叠状态，需要再点一次展开
  // 修复方案: 包装 toggleMode，切换到 five-space 时自动展开工作区面板
  const layoutMode = useLayoutModeStore((s) => s.mode)
  const rawToggleMode = useLayoutModeStore((s) => s.toggleMode)
  const toggleLayoutMode = useCallback(() => {
    const currentMode = useLayoutModeStore.getState().mode
    rawToggleMode()
    if (currentMode === 'classic') {
      useUIStore.getState().setWorkspaceCollapsed(false)
    }
  }, [rawToggleMode])

  const {
    sessions,
    activeSessionId,
    wsStatus,
    isLoading: isSessionLoading,
    connectWebSocket,
    disconnectWebSocket,
  } = useSessionStore()
  const { createSession, setActiveSession, deleteSession, copySession, toggleSessionStar, toggleSessionPin, renameSession, fetchSessions } = useSessionListStore()
  const { isStreaming, stopStreamingForTab, streamingTabs } = useStreamingStore()

  /** 侧边栏是否折叠 (from global UI store, shared with AppHeader) */
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  /** 默认模型名 */
  const [modelName, setModelName] = useState('glm-5.1')

  // 加载默认模型配置
  useEffect(() => {
    fetch('/api/v1/config/llm/defaults')
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.chat) setModelName(data.chat)
      })
      .catch(() => {})
  }, [])

  /**
   * 当前活跃会话的消息列表（从 pipelineMessageStore 读取）
   *
   * BUG-FIX-fix_20260513_msg_not_realtime:
   * 问题根因: useMemo 只依赖 activeSessionId，但内部用 session ID 而非 pipeline ID
   *          作为 getMessages 的 key，导致在主管道场景下永远返回空数组。
   * 修复方案: 改用 activePipelineId 作为主要查询 key，并添加为依赖项，
   *          确保管道切换时重新计算消息列表。
   * 影响范围: ChatContainer 接收的 messages prop
   * 修复日期: 2026-05-13
   */
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const activeMessages = useMemo(
    () => {
      const pid = activePipelineId || activeSessionId
      return pid ? usePipelineMessageStore.getState().getMessages(pid) : []
    },
    [activePipelineId, activeSessionId],
  )

  /** 当前活跃会话的分页状态（从 pipelineMessageStore 响应式读取） */
  const activeKey = activePipelineId || activeSessionId
  const hasMoreMessages = usePipelineMessageStore((s) => activeKey ? (s.hasMoreOlderByPipeline[activeKey] ?? false) : false)
  const isLoadingMoreMessages = usePipelineMessageStore((s) => activeKey ? (s.isLoadingOlderByPipeline[activeKey] ?? false) : false)

  // ------------------------------------------
  // 初始化：加载会话列表
  // ------------------------------------------
  useEffect(() => {
    fetchSessions().catch(console.error)
  }, [fetchSessions])

  // ------------------------------------------
  // 初始化全局流式事件处理器（不随组件卸载而销毁）
  // ------------------------------------------
  useEffect(() => {
    initStreamingEvents()
    return () => {
      destroyStreamingEvents()
    }
  }, [])

  // ------------------------------------------
  // 页面刷新后恢复 WS 连接
  // 会话状态从 localStorage 恢复后需要重新建立全局 WS 连接
  // ------------------------------------------
  useEffect(() => {
    const currentToken = useAuthStore.getState().token
    if (currentToken) {
      globalWS.connect(currentToken)
      useSessionStore.setState({ wsStatus: globalWS.status })
    }

    const handleStatusChange = (data: { status: string }) => {
      useSessionStore.setState({ wsStatus: data.status as any })
    }
    globalWS.subscribe('_status', handleStatusChange)
    return () => {
      globalWS.unsubscribe('_status', handleStatusChange)
    }
  }, [])

  /**
   * 选择会话
   *
   * 设置活跃会话并建立 WebSocket 连接。
   * setActiveSession 会自动加载历史消息。
   * 切换前保存当前会话的 Tab 状态，避免数据丢失。
   * 移动端下选择会话后自动收起侧边栏。
   */
  const handleSelectSession = useCallback(
    async (sessionId: string) => {
      // 保存当前会话的 Tab 状态到 localStorage
      useAgentTabStore.getState().saveCurrentTabs()

      await setActiveSession(sessionId)
      const currentToken = useAuthStore.getState().token
      if (currentToken) {
        connectWebSocket(sessionId, currentToken)
      }

      // 移动端选择会话后自动收起侧边栏
      if (isMobileViewport()) {
        useUIStore.getState().setSidebarCollapsed(true)
      }
    },
    [setActiveSession, connectWebSocket],
  )

  /**
   * 创建新会话并自动选中
   */
  const handleCreateSession = useCallback(async () => {
    try {
      const newSession = await createSession()
      await handleSelectSession(newSession.id)
    } catch (error) {
      console.error('创建会话失败:', error)
    }
  }, [createSession, handleSelectSession])

  /**
   * 发送消息
   *
   * 1. 将用户消息添加到本地状态（主 Tab 写入 sessionStore，子 Tab 写入 agentTabStore）
   * 2. 通过 WebSocket 发送用户输入，子 Tab 时携带 pipelineId 路由到对应管道
   */
  const handleSendMessage = useCallback(
    async (params: SendMessageParams) => {
      const { activeSessionId: sid } = useSessionStore.getState()
      const currentToken = useAuthStore.getState().token

      if (!sid || !currentToken) {
        return
      }

      const listStore = useSessionListStore.getState()
      const sessions = listStore.sessions || []
      const session = sessions.find(s => s.id === sid)
      if (session && (!session.title || session.title.startsWith('新会话'))) {
        listStore.renameSession(sid, params.content.slice(0, 50))
      }

      // BUG-FIX-fix_20260520_task_submit_fail:
      // 问题根因: 新会话首次发消息时后端尚未创建 pipeline，activePipelineId 为空，
      //           导致 if (!activePipelineId) return 静默丢弃消息，用户看到"提交任务失败"。
      // 修复方案: 以 activeSessionId 作为 fallback pipelineId，确保消息始终能写入本地状态
      //           并通过 WebSocket 发送到后端。后端创建 pipeline 后会通过 WS 事件通知前端更新。
      // 影响范围: 新会话首次消息发送
      const pipelineStore = usePipelineMessageStore.getState()
      const activePipelineId = pipelineStore.activePipelineId || sid

      const existingMsgs = pipelineStore.getMessages(activePipelineId)
      const nextSeq = existingMsgs.reduce((max, m) => Math.max(max, m.sequence ?? 0), 0) + 1

      const userMessage: Message = {
        id: generateUUID(),
        sessionId: sid,
        role: 'user',
        content: params.content,
        timestamp: new Date().toISOString(),
        parentId: null,
        sequence: nextSeq,
      }

      pipelineStore.addMessage(activePipelineId, userMessage)

      const targetPipelineId = params.pipelineId || activePipelineId
      const enteredInteraction =
        useInteractionStore.getState().getEnteredForPipeline(targetPipelineId) ||
        useInteractionStore.getState().getEnteredForPipeline(sid)
      if (enteredInteraction) {
        globalWS.sendInteractionResponse(sid, enteredInteraction.requestId, {
          responseType: 'approved',
          feedback: '用户已到达对话页面',
        })
        useInteractionStore.getState().markResponded(enteredInteraction.requestId)
      }

      try {
        await globalWS.sendUserInput(
          sid,
          params.content,
          {
            enableThinking: params.enableThinking,
            pipelineId: params.pipelineId,
            clientMessageId: userMessage.id,
          },
        )
      } catch {
        // WebSocket 发送失败时消息已添加到本地状态，重连后会自动重试
      }
    },
    [],
  )

  /**
   * 停止生成
   *
   * BUG-FIX-fix_20260509_tab_streaming: 使用 activePipelineId 停止 streaming
   */
  const handleStopGenerate = useCallback(() => {
    const sid = useSessionStore.getState().activeSessionId
    const currentPipelineId = usePipelineMessageStore.getState().activePipelineId
    if (sid) {
      globalWS.sendCancel(sid, undefined, currentPipelineId || undefined)
    }
    if (currentPipelineId) {
      // 刷写缓冲区中残留的 chunk，避免数据丢失
      flushStreamChunkBuffer()
      // 清理 chunkTimeout 计时器
      clearChunkTimeout(currentPipelineId)
      // 清理 streamingTabs（UI 层 streaming 状态）
      stopStreamingForTab(currentPipelineId)
      // 清理消息层面的 streaming 状态，标记消息为 completed
      usePipelineMessageStore.getState().stopStreaming(currentPipelineId)
    }
  }, [stopStreamingForTab])

  /**
   * 登出并跳转到登录页
   */
  const handleLogout = useCallback(async () => {
    destroyStreamingEvents()
    disconnectWebSocket()
    globalWS.disconnect()
    await logout()
    navigate(ROUTES.LOGIN)
  }, [logout, navigate, disconnectWebSocket])

  /** WebSocket 连接状态 */
  const isWsConnected = wsStatus === 'connected'

  // ---- Render sidebar content (shared between layouts) ----
  const sidebarContent = (
    <>
      <div className="shrink-0 border-b p-2.5">
        <button
          onClick={handleCreateSession}
          className="bg-primary text-primary-foreground w-full rounded-lg px-3 py-2 text-sm font-medium transition-opacity hover:opacity-90"
        >
          + 新会话
        </button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {sessions.length === 0 ? (
          <div className="text-muted-foreground p-4 text-center text-sm">
            暂无会话
            <br />
            <span className="text-xs">点击上方按钮创建</span>
          </div>
        ) : (
          sessions.map((session) => (
            <div
              key={session.id}
              className={`group relative flex items-center transition-colors ${
                activeSessionId === session.id
                  ? 'bg-accent text-accent-foreground font-medium'
                  : 'hover:bg-accent/50 text-foreground/80'
              }`}
            >
              <div
                onClick={() => handleSelectSession(session.id)}
                className="min-w-0 flex-1 cursor-pointer truncate px-3 py-2.5 text-sm"
                title={session.title}
              >
                {session.title}
              </div>
              <div
                className={`ml-1 mr-1 flex flex-shrink-0 items-center gap-0.5 transition-opacity ${
                  activeSessionId === session.id
                    ? 'opacity-100'
                    : 'opacity-0 group-hover:opacity-100'
                }`}
              >
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      onClick={(e) => e.stopPropagation()}
                      className="text-muted-foreground hover:text-foreground rounded p-0.5 transition-colors"
                      aria-label="更多操作"
                    >
                      <MoreHorizontal className="h-3.5 w-3.5" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-[140px]">
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        const newTitle = window.prompt('重命名会话', session.title)
                        if (newTitle?.trim()) {
                          renameSession(session.id, newTitle.trim())
                        }
                      }}
                    >
                      <Pencil className="mr-2 h-3.5 w-3.5" /> 重命名
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        copySession(session.id)
                      }}
                    >
                      <Copy className="mr-2 h-3.5 w-3.5" /> 复制
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleSessionStar(session.id)
                      }}
                    >
                      <Star className="mr-2 h-3.5 w-3.5" />
                      {session.starred ? '取消星标' : '星标'}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleSessionPin(session.id)
                      }}
                    >
                      <Pin className="mr-2 h-3.5 w-3.5" />
                      {session.pinned ? '取消置顶' : '置顶会话'}
                    </DropdownMenuItem>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        if (window.confirm('确定要删除此会话吗？')) {
                          deleteSession(session.id).catch(() => {})
                        }
                      }}
                      className="text-destructive focus:text-destructive"
                    >
                      <Trash2 className="mr-2 h-3.5 w-3.5" /> 删除
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>
          ))
        )}
      </div>
    </>
  )

  // ---- Render chat content (shared between layouts) ----
  const chatContent = activeSessionId ? (
    <ChatContainer
      sessionId={activeSessionId}
      messages={activeMessages}
      isLoading={isSessionLoading}
      // NOTE: ChatContainer 内部使用 effectiveIsGenerating (基于 activePipelineId)
      // 此 prop 仅作兼容保留，实际不影响输入框状态
      isGenerating={activeSessionId ? (streamingTabs[usePipelineMessageStore.getState().activePipelineId || ''] ?? false) : false}
      modelName={modelName}
      onSendMessage={handleSendMessage}
      onStopGenerate={handleStopGenerate}
      hasMoreMessages={hasMoreMessages}
      isLoadingMoreMessages={isLoadingMoreMessages}
      onLoadMoreMessages={() => {
        const pid = usePipelineMessageStore.getState().activePipelineId
        const sid = useSessionStore.getState().activeSessionId
        if (pid) {
          const topCursor = usePipelineMessageStore.getState().getTopCursor(pid)
          usePipelineMessageStore.getState().fetchMessages(pid, { before_sequence: topCursor, threadId: sid || undefined })
        }
      }}
      className="flex-1"
    />
  ) : (
    <div className="text-foreground flex flex-1 flex-col items-center justify-center gap-6 px-8">
      <div className="flex flex-col items-center gap-3">
        <div className="bg-primary/10 text-primary flex h-16 w-16 items-center justify-center rounded-2xl text-3xl">
          <svg className="h-8 w-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
        </div>
        <h2 className="text-3xl font-bold tracking-tight">欢迎使用超级终端</h2>
        <p className="text-muted-foreground max-w-sm text-center text-base">
          选择左侧已有会话，或创建新会话开始对话
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <button
          onClick={handleCreateSession}
          className="bg-primary text-primary-foreground rounded-lg px-5 py-2.5 text-sm font-medium transition-opacity hover:opacity-90"
        >
          + 新会话
        </button>
        <a
          href={ROUTES.AGENTS}
          className="text-foreground border-border hover:bg-accent rounded-lg border px-5 py-2.5 text-sm transition-colors"
        >
          浏览智能体
        </a>
      </div>
    </div>
  )

  // ---- Five-space layout mode ----
  if (layoutMode === 'five-space') {
    return (
      <FiveSpaceLayout
        chatContent={chatContent}
        sidebarContent={sidebarContent}
        onToggleMode={toggleLayoutMode}
        showThemePanel={showThemePanel}
        onShowThemePanel={setShowThemePanel}
        onLogout={handleLogout}
      />
    )
  }

  // ---- Classic layout mode (original) ----
  return (
    <div className="bg-background text-foreground flex h-screen flex-col">
      <AppHeader
        onToggleMode={toggleLayoutMode}
        modeLabel="Five-space"
        showThemePanel={showThemePanel}
        onShowThemePanel={setShowThemePanel}
        onLogout={handleLogout}
      />

      <div className="relative flex min-h-0 flex-1">
        {/* 移动端侧边栏：覆盖抽屉模式（从导航栏下方开始，不遮盖导航栏） */}
        {!sidebarCollapsed && (
          <div className="fixed left-0 right-0 bottom-0 z-40 md:hidden" style={{ top: 40 }}>
            {/* 背景遮罩，点击关闭侧边栏 */}
            <div
              className="absolute inset-0 bg-black/50"
              onClick={() => useUIStore.getState().setSidebarCollapsed(true)}
            />
            {/* 侧边栏面板 */}
            <aside className="absolute left-0 top-0 bottom-0 z-50 flex w-72 flex-col border-r bg-background shadow-xl">
              {sidebarContent}
            </aside>
          </div>
        )}

        {/* 桌面端侧边栏：内嵌模式（>= md 断点） */}
        <aside
          className={`${
            sidebarCollapsed ? 'w-0' : 'w-56'
          } hidden shrink-0 flex-col overflow-hidden border-r transition-all duration-200 md:flex`}
        >
          {sidebarContent}
        </aside>

        {/* 主内容区：移动端占满全宽 */}
        <main className="flex min-h-0 flex-1 flex-col">
          {chatContent}
        </main>
      </div>
    </div>
  )
}

// ============================================
// 路由器创建
// ============================================

/**
 * 创建路由器实例
 *
 * 路由结构：
 * - / : 受保护的聊天主页（需登录）
 * - /settings : 设置中心（懒加载）
 * - /settings/modules : 模块设置页（懒加载）
 * - /settings/api : API 配置页（懒加载）
 * - /settings/llm : LLM 模型配置页（懒加载）
 * - /settings/context : 上下文窗口配置页（懒加载）
 * - /settings/concurrency : 并发控制配置页（懒加载）
 * - /settings/cost : 成本控制配置页（懒加载）
 * - /tools : 工具管理（懒加载）
 * - /agents : 智能体管理（懒加载）
 * - /monitoring : 系统监控（懒加载）
 * - /admin : 管理员面板（懒加载）
 * - /memory : 记忆管理（懒加载）
 * - /debug : 调试中心（懒加载）
 * - /debug/execution-records : 执行记录（懒加载）
 * - /debug/sessions : 调试会话（懒加载）
 * - /debug/tasks : 调试任务（懒加载）
 * - /debug/evaluation-metrics : 评估指标（懒加载）
 * - /debug/users : 调试用户（懒加载）
 * - /login : 登录页
 * - /register : 注册页
 * - * : 兜底重定向到首页
 */
export function createRouter() {
  return createBrowserRouter([
    {
      path: ROUTES.HOME,
      element: (
        <ProtectedRoute>
          <HomePage />
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <SettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: '/settings/modules',
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ModulesSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_API,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ApiSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_LLM,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <LlmSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_CONTEXT,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ContextWindowSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_CONCURRENCY,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ConcurrencySettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_COST,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <CostSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_PLUGINS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <PluginsSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.TOOLS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ToolsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.AGENTS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <AgentsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.MONITORING,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <MonitoringPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.ADMIN,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <AdminPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.MEMORY,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <MemoryPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.TRIGGERS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <TriggersPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.KNOWLEDGE_BASE,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <KnowledgeBasePage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.ROOT,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.EXECUTION_RECORDS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugExecutionRecordsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.SESSIONS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugSessionsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.TASKS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugTasksPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.EVALUATION_METRICS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugEvaluationMetricsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.USERS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugUsersPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.LOGIN,
      element: <LoginPage />,
    },
    {
      path: ROUTES.REGISTER,
      element: <RegisterPage />,
    },
    {
      path: '*',
      element: <Navigate to={ROUTES.HOME} replace />,
    },
  ])
}
