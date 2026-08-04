/** 路由配置 定义应用的所有路由，包含登录/注册和受保护的主页面。 */

import { lazy, Suspense, useEffect, useState, useCallback } from 'react'
import { createBrowserRouter, Navigate, useNavigate, useLocation } from 'react-router-dom'
import { ChatContainer } from './components/chat/ChatContainer'
import { GlobalInteractionOverlay } from './components/chat/GlobalInteractionOverlay'
import { AppHeader } from './components/layout/AppHeader'
import { FiveSpaceLayout } from './components/layout/FiveSpaceLayout'
import { SessionEditModal } from './components/session/SessionEditModal'
import { SessionList } from './components/session/SessionList'
import { ROUTES } from './constants/routes'
import { useConnectionStatus } from './hooks/useConnectionStatus'
import { useRealtimeEvents } from './hooks/useRealtimeEvents'
import { useTaskPolling } from './hooks/useTaskPolling'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { globalWS } from './services/websocket/GlobalWebSocket'
import { initStreamingEvents, destroyStreamingEvents } from './services/websocket/streamingEventService'
import { flushStreamChunkBuffer } from './services/websocket/streaming/handlers/streamHandler'
import { allocateNextSequence, ensureStreamingPlaceholder } from './services/websocket/streaming/handlers/utils'
import { useAgentStore } from './stores/agentStore'
import { useAgentTabStore } from './stores/agentTabStore'
import { useAuthStore } from './stores/authStore'
import { useInteractionStore } from './stores/interactionStore'
import { useLayoutModeStore } from './stores/layoutModeStore'
import { usePipelineMessageStore } from './stores/pipelineMessageStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useSessionStore } from './stores/sessionStore'
import { useUIStore } from './stores/uiStore'
import { generateUUID } from './utils/uuid'
import type { SendMessageParams } from './components/chat/types'
import type { Session } from './types'
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
const MemorySettingsPage = lazy(() =>
  import('@/pages/settings/MemorySettingsPage').then((m) => ({
    default: m.MemorySettingsPage,
  })),
)
const IsolationSettingsPage = lazy(() =>
  import('@/pages/settings/IsolationSettingsPage').then((m) => ({
    default: m.IsolationSettingsPage,
  })),
)
const SecuritySettingsPage = lazy(() =>
  import('@/pages/settings/SecuritySettingsPage').then((m) => ({
    default: m.SecuritySettingsPage,
  })),
)
const EvaluationSettingsPage = lazy(() =>
  import('@/pages/settings/EvaluationSettingsPage').then((m) => ({
    default: m.EvaluationSettingsPage,
  })),
)
const ExternalToolsSettingsPage = lazy(() =>
  import('@/pages/settings/ExternalToolsSettingsPage').then((m) => ({
    default: m.ExternalToolsSettingsPage,
  })),
)
const PipelineSettingsPage = lazy(() =>
  import('@/pages/settings/PipelineSettingsPage').then((m) => ({
    default: m.PipelineSettingsPage,
  })),
)
const ThemeSettingsPage = lazy(() =>
  import('@/pages/settings/ThemeSettingsPage').then((m) => ({
    default: m.ThemeSettingsPage,
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
const GenericConfigRoute = lazy(() =>
  import('@/pages/settings/GenericConfigRoute').then((m) => ({
    default: m.GenericConfigRoute,
  })),
)

/** 懒加载 fallback */
const LazyFallback = <div className="text-muted-foreground p-4">加载中...</div>

/** 判断当前视口是否为移动端（< md 断点 768px） */
function isMobileViewport(): boolean {
  return typeof window !== 'undefined' && window.innerWidth < 768
}

// 路由守卫

/** 路由守卫组件 检查用户认证状态： */
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

  return (
    <>
      {children}
      {/* 全局交互浮层：在所有受保护页面中显示待处理交互 */}
      <GlobalInteractionOverlay />
    </>
  )
}

// 聊天主页

/** 聊天主页组件 登录后的主界面，包含： */
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
  const layoutMode = useLayoutModeStore((s) => s.mode)
  const rawToggleMode = useLayoutModeStore((s) => s.toggleMode)
  const toggleLayoutMode = useCallback(() => {
    const currentMode = useLayoutModeStore.getState().mode
    rawToggleMode()
    if (currentMode === 'classic') {
      useUIStore.getState().setWorkspaceCollapsed(false)
    }
  }, [rawToggleMode])

  const sessions = useSessionStore((s) => s.sessions)
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const wsStatus = useSessionStore((s) => s.wsStatus)
  const isSessionLoading = useSessionStore((s) => s.isLoading)
  const connectWebSocket = useSessionStore((s) => s.connectWebSocket)
  const disconnectWebSocket = useSessionStore((s) => s.disconnectWebSocket)
  const createSession = useSessionListStore((s) => s.createSession)
  const setActiveSession = useSessionListStore((s) => s.setActiveSession)
  const deleteSession = useSessionListStore((s) => s.deleteSession)
  const copySession = useSessionListStore((s) => s.copySession)
  const toggleSessionStar = useSessionListStore((s) => s.toggleSessionStar)
  const toggleSessionPin = useSessionListStore((s) => s.toggleSessionPin)
  const renameSession = useSessionListStore((s) => s.renameSession)
  const updateSessionAgent = useSessionListStore((s) => s.updateSessionAgent)
  const fetchSessions = useSessionListStore((s) => s.fetchSessions)

  /** 侧边栏是否折叠 (from global UI store, shared with AppHeader) */
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed)

  /** 确保 Agent 配置列表已加载（ChatContainer 按 activeTab.agentId 解析当前管道模型） */
  const fetchAgents = useAgentStore((s) => s.fetchAgents)

  useEffect(() => {
    fetchAgents().catch(() => {})
  }, [fetchAgents])

  /** 当前活跃会话的消息列表（从 pipelineMessageStore 响应式读取） */
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)

  /** 当前活跃会话的分页状态（从 pipelineMessageStore 响应式读取） */
  const activeKey = activePipelineId
  const hasMoreMessages = usePipelineMessageStore((s) => activeKey ? (s.hasMoreOlderByPipeline[activeKey] ?? false) : false)
  const isLoadingMoreMessages = usePipelineMessageStore((s) => activeKey ? (s.isLoadingOlderByPipeline[activeKey] ?? false) : false)

  // 初始化：加载会话列表
  useEffect(() => {
    fetchSessions().catch(console.error)
  }, [fetchSessions])

  // 初始化全局流式事件处理器（不随组件卸载而销毁）
  useEffect(() => {
    initStreamingEvents()
    return () => {
      destroyStreamingEvents()
    }
  }, [])

  // 页面刷新后恢复 WS 连接
  // 会话状态从 localStorage 恢复后需要重新建立全局 WS 连接
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

  /** 选择会话 设置活跃会话并建立 WebSocket 连接。 */
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

  /** 创建新会话并自动选中 */
  const handleCreateSession = useCallback(async () => {
    try {
      const newSession = await createSession()
      await handleSelectSession(newSession.id)
    } catch (error) {
      console.error('创建会话失败:', error)
    }
  }, [createSession, handleSelectSession])

  /** 发送消息 1. 将用户消息添加到本地状态（主 Tab 写入 sessionStore，子 Tab 写入 agentTabStore） */
  const handleSendMessage = useCallback(
    async (params: SendMessageParams) => {
      const { activeSessionId: sid } = useSessionStore.getState()
      const currentToken = useAuthStore.getState().token

      if (!sid || !currentToken) {
        return
      }

      const listStore = useSessionListStore.getState()
      const sessions = useSessionStore.getState().sessions || []
      const session = sessions.find(s => s.id === sid)
      if (session && (session.title === '灵汐' || session.title === '新会话')) {
        const title = params.content.replace(/\n/g, ' ').trim().slice(0, 30)
        if (title) {
          listStore.renameSession(sid, title)
        }
      }

      const pipelineStore = usePipelineMessageStore.getState()
      let activePipelineId = pipelineStore.activePipelineId

      // -fix_empty_pipeline_id_on_send:
      if (!activePipelineId) {
        const sessions = useSessionStore.getState().sessions
        const session = sessions.find((s) => s.id === sid)
        let fallbackPipelineId = session?.pipelineIds?.[0] || session?.activePipelineId
        // sessionStore 中没有时，从 agentTabStore 持久化数据中获取 pipelineRunId
        if (!fallbackPipelineId) {
          try {
            const raw = localStorage.getItem(`agent-tabs-${sid}`)
            if (raw) {
              const data = JSON.parse(raw)
              const tab = data.tabs?.find((t: { agentLevel: number }) => t.agentLevel === 1) || data.tabs?.[0]
              if (tab?.pipelineRunId) {
                fallbackPipelineId = tab.pipelineRunId
              }
            }
          } catch { /* 忽略解析错误 */ }
        }
        if (fallbackPipelineId) {
          if (!pipelineStore.pipelines[fallbackPipelineId]) {
            pipelineStore.registerPipeline({
              pipelineId: fallbackPipelineId,
              sessionId: sid,
              level: 1,
              tabId: null,
              agentName: '',
              status: 'idle',
              parentId: null,
              unreadCount: 0,
            })
          }
          pipelineStore.activatePipeline(fallbackPipelineId)
          activePipelineId = fallbackPipelineId
        } else {
          return
        }
      }

      const targetPipelineId = params.pipelineId || activePipelineId

      const existingMsgs = pipelineStore.getMessages(targetPipelineId)

      const userMessageId = generateUUID()
      const userMessage: Message = {
        id: userMessageId,
        sessionId: sid,
        role: 'user',
        content: params.content,
        timestamp: new Date().toISOString(),
        sequence: allocateNextSequence(targetPipelineId),
        status: 'completed',
        clientMessageId: userMessageId,
        parentId: null,
        attachments: params.attachments?.map((att) => ({
          id: att.id,
          name: att.name,
          type: att.type,
          url: att.url,
        })),
      }

      pipelineStore.addMessage(targetPipelineId, userMessage)
      const enteredInteraction =
        useInteractionStore.getState().getEnteredForPipeline(targetPipelineId) ||
        useInteractionStore.getState().getEnteredForPipeline(sid)
      if (enteredInteraction) {
        globalWS.sendInteractionResponse(sid, enteredInteraction.requestId, {
          response_type: 'approved',
          feedback: '用户已到达对话页面',
        })
        useInteractionStore.getState().markResponded(enteredInteraction.requestId)
      }

      // 发送前立即创建"思考中"占位气泡，让用户点发送的瞬间就看到反馈，
      // 而不是等到 stream_start（后端管道已接收并开始流式）才出现气泡。
      // globalWS.sendUserInput 是同步入队（_send 永不抛异常：已连接则 ws.send，否则入队待重连），
      // 因此占位气泡放在 send 之前同步创建即可，不存在"发送失败需回滚占位气泡"的情况。
      // 使用临时 placeholder_ 前缀 ID，后续 stream_start 事件到达时，
      // utils.ensureStreamingPlaceholder 会通过 updateMessage(prevMsg.id, { id: realMessageId })
      // 将此占位气泡的 ID 改写为后端真实 messageId（utils.ts 合并分支）。
      const placeholderMsgId = `placeholder_${generateUUID()}`
      ensureStreamingPlaceholder(targetPipelineId, placeholderMsgId, sid)

      globalWS.sendUserInput(sid, params.content, {
        enableThinking: params.enableThinking,
        pipelineId: targetPipelineId,
        clientMessageId: userMessage.id,
        attachments: params.attachments?.map((att) => ({
          file_id: att.id,
          filename: att.name,
          mime_type: att.type,
          media_type: att.type?.startsWith('image/') ? 'image' : att.type?.startsWith('audio/') ? 'audio' : att.type?.startsWith('video/') ? 'video' : 'document',
          size: att.size || 0,
          url: att.url,
        })),
      })
    },
    [],
  )

  /** 停止生成 */
  const handleStopGenerate = useCallback(() => {
    const sid = useSessionStore.getState().activeSessionId
    const currentPipelineId = usePipelineMessageStore.getState().activePipelineId
    if (sid) {
      globalWS.sendCancel(sid, undefined, currentPipelineId || undefined)
    }
    if (currentPipelineId) {
      flushStreamChunkBuffer()
      usePipelineMessageStore.getState().stopStreaming(currentPipelineId)
    }
  }, [])

  /** 登出并跳转到登录页 */
  const handleLogout = useCallback(async () => {
    destroyStreamingEvents()
    disconnectWebSocket()
    globalWS.disconnect()
    await logout()
    navigate(ROUTES.LOGIN)
  }, [logout, navigate, disconnectWebSocket])

  // 编辑会话模态框（支持切换 Agent）
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null)

  const handleEditSession = useCallback((session: Session) => {
    setEditingSessionId(session.id)
  }, [])

  const handleCloseEditModal = useCallback(() => {
    setEditingSessionId(null)
  }, [])

  const handleSaveEdit = useCallback(
    async (sessionId: string, title: string, agentId: string | null) => {
      try {
        renameSession(sessionId, title)
        await updateSessionAgent(sessionId, agentId)
        setEditingSessionId(null)
      } catch (error) {
        console.error('保存编辑失败:', error)
      }
    },
    [renameSession, updateSessionAgent],
  )

  const editingSession = editingSessionId
    ? sessions.find((s) => s.id === editingSessionId) || null
    : null

  // Render sidebar content (shared between layouts)
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

      <div className="min-h-0 flex-1 overflow-y-auto">
        <SessionList
          sessions={sessions}
          activeSessionId={activeSessionId}
          deletingSessionIds={new Set<string>()}
          onSessionClick={handleSelectSession}
          onDeleteSession={(id) => { if (window.confirm('确定要删除此会话吗？')) deleteSession(id).catch(() => {}) }}
          onEditSession={handleEditSession}
          onCopySession={(session) => copySession(session.id)}
          onStarSession={(id) => toggleSessionStar(id)}
          onPinSession={(id) => toggleSessionPin(id)}
        />
      </div>

      <SessionEditModal
        mode="edit"
        isOpen={!!editingSessionId}
        session={editingSession}
        onClose={handleCloseEditModal}
        onSave={handleSaveEdit}
      />
    </>
  )

  // Render chat content (shared between layouts)
  const chatContent = activeSessionId ? (
    <ChatContainer
      sessionId={activeSessionId}
      isLoading={isSessionLoading}
      // NOTE: ChatContainer 内部使用 effectiveIsGenerating (基于 activePipelineId)
      // 此 prop 仅作兼容保留，实际不影响输入框状态
      isGenerating={false}
      onSendMessage={handleSendMessage}
      onStopGenerate={handleStopGenerate}
      hasMoreMessages={hasMoreMessages}
      isLoadingMoreMessages={isLoadingMoreMessages}
      onLoadMoreMessages={() => {
        const store = usePipelineMessageStore.getState()
        const pid = store.activePipelineId
        const sid = useSessionStore.getState().activeSessionId
        if (!pid) return
        if (!store.hasMoreOlderByPipeline[pid]) return
        if (store.isLoadingOlderByPipeline[pid]) return
        const topCursor = store.getTopCursor(pid)
        store.fetchMessages(pid, { before_sequence: topCursor, threadId: sid || undefined })
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

  // Five-space layout mode
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

  // Classic layout mode (original)
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

// 路由器创建

/** 创建路由器实例 路由结构： */
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
      path: ROUTES.SETTINGS_MEMORY,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <MemorySettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_ISOLATION,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <IsolationSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_SECURITY,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <SecuritySettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_EVALUATION,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <EvaluationSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_EXTERNAL_TOOLS,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ExternalToolsSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_PIPELINE,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <PipelineSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.SETTINGS_THEME,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <ThemeSettingsPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: '/settings/generic/*',
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <GenericConfigRoute />
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
