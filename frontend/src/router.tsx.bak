/**
 * 路由配置
 *
 * 定义应用的所有路由，包含登录/注册和受保护的主页面。
 * 主页包含完整的聊天界面：左侧会话列表 + 右侧聊天区域。
 */

import { lazy, Suspense, useEffect, useState, useCallback, useMemo } from 'react'
import { createBrowserRouter, Navigate, useNavigate } from 'react-router-dom'
import { LayoutGrid } from 'lucide-react'
import { ChatContainer } from './components/chat/ChatContainer'
import { ROUTES } from './constants/routes'
import { WS_SERVER_EVENTS } from './constants/websocket'
import { useMessageActions } from './hooks/useMessageActions'
import { useConnectionStatus } from './hooks/useConnectionStatus'
import { useRealtimeEvents } from './hooks/useRealtimeEvents'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { webSocketService } from './services/websocket/WebSocketService'
import { useAuthStore } from './stores/authStore'
import { useLayoutModeStore } from './stores/layoutModeStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useSessionStore } from './stores/sessionStore'
import { useStreamingStore } from './stores/streamingStore'
import { FiveSpaceLayout } from './components/layout/FiveSpaceLayout'
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

/** 懒加载 fallback */
const LazyFallback = <div className="text-muted-foreground p-4">加载中...</div>

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
      <div className="bg-background flex min-h-screen items-center justify-center">
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
  const { user, token, logout } = useAuthStore()

  // Phase 1 hooks: connection status and real-time events
  useConnectionStatus()
  useRealtimeEvents()

  // Layout mode toggle
  const layoutMode = useLayoutModeStore((s) => s.mode)
  const toggleLayoutMode = useLayoutModeStore((s) => s.toggleMode)

  const {
    sessions,
    activeSessionId,
    messages,
    wsStatus,
    isLoading: isSessionLoading,
    connectWebSocket,
    disconnectWebSocket,
    getActiveSessionMessages,
    addMessage,
    updateMessageContent,
    updateMessageFields,
    getMessagePagination,
    loadMoreMessages,
  } = useSessionStore()
  const { fetchSessions, createSession, setActiveSession, deleteSession } = useSessionListStore()
  const { isStreaming, setStreaming, stopStreaming } = useStreamingStore()

  /** 消息操作 hooks */
  const messageActions = useMessageActions(activeSessionId ?? undefined)

  /** 侧边栏是否折叠 */
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

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

  /** 当前活跃会话的消息列表（响应式） */
  const activeMessages = useMemo(
    () => (activeSessionId ? messages[activeSessionId] || [] : []),
    [activeSessionId, messages],
  )

  /** 当前活跃会话的分页状态 */
  const pagination = activeSessionId ? getMessagePagination(activeSessionId) : null

  // ------------------------------------------
  // 初始化：加载会话列表
  // ------------------------------------------
  useEffect(() => {
    fetchSessions().catch(console.error)
  }, [fetchSessions])

  // ------------------------------------------
  // 订阅 WebSocket 流式事件
  // ------------------------------------------
  useEffect(() => {
    /**
     * 处理流式开始事件
     *
     * 创建一条空的 assistant 消息占位，后续 chunk 会逐步填充内容。
     * 事件数据格式：{ data: { message_id, session_id, ... } } 或扁平格式
     */
    const handleStreamStart = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return

      const messageId = eventData.message_id || eventData.data?.message_id
      if (!messageId) return

      setStreaming(true)
      addMessage(sid, {
        id: messageId,
        sessionId: sid,
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
        parentId: null,
        sequence: 0,
        status: 'streaming',
      })
    }

    /**
     * 处理流式内容块事件
     *
     * 将增量内容追加到对应的 assistant 消息。
     * 事件数据格式：{ data: { message_id, content, ... } } 或扁平格式
     */
    const handleStreamChunk = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return

      const messageId = eventData.message_id || eventData.data?.message_id
      const content = eventData.content || eventData.data?.content || ''
      if (!messageId) return

      updateMessageContent(sid, messageId, content, { mode: 'append' })
    }

    /**
     * 处理流式结束事件
     *
     * 标记流式生成完成
     */
    const handleStreamEnd = (_eventData: any) => {
      setStreaming(false)
    }

    /**
     * 处理新消息事件
     *
     * 收到完整的最终消息，确保流式状态结束
     */
    const handleNewMessage = (_eventData: any) => {
      setStreaming(false)
    }

    // 订阅所有流式相关事件
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
    webSocketService.subscribe(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)

    // --- Thinking 事件 ---
    const handleThinkingStart = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      const messageId = eventData.message_id || eventData.data?.message_id
      if (!messageId) return
      updateMessageFields(sid, messageId, {
        thinking: { content: '', isThinking: true },
      })
    }

    const handleThinkingChunk = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      const messageId = eventData.message_id || eventData.data?.message_id
      const chunk = eventData.content || eventData.data?.content || ''
      if (!messageId || !chunk) return

      const msgs = useSessionStore.getState().messages[sid] || []
      const msg = msgs.find((m) => m.id === messageId)
      const prevContent = msg?.thinking?.content || ''
      updateMessageFields(sid, messageId, {
        thinking: { content: prevContent + chunk, isThinking: true },
      })
    }

    const handleThinkingEnd = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      const messageId = eventData.message_id || eventData.data?.message_id
      if (!messageId) return
      const msgs = useSessionStore.getState().messages[sid] || []
      const msg = msgs.find((m) => m.id === messageId)
      updateMessageFields(sid, messageId, {
        thinking: { content: msg?.thinking?.content || '', isThinking: false },
      })
    }

    webSocketService.subscribe(WS_SERVER_EVENTS.THINKING_START, handleThinkingStart)
    webSocketService.subscribe(WS_SERVER_EVENTS.THINKING_CHUNK, handleThinkingChunk)
    webSocketService.subscribe(WS_SERVER_EVENTS.THINKING_END, handleThinkingEnd)

    // --- Tool 事件 ---
    const handleToolStart = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      const messageId = eventData.message_id || eventData.data?.message_id
      const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
      if (!messageId) return

      const msgs = useSessionStore.getState().messages[sid] || []
      const msg = msgs.find((m) => m.id === messageId)
      const existing = msg?.toolCalls || []
      const callId = eventData.call_id || eventData.data?.call_id || `call_${Date.now()}`
      updateMessageFields(sid, messageId, {
        toolCalls: [...existing, {
          call_id: callId,
          tool_name: toolName,
          tool_args: eventData.args || eventData.data?.args || {},
          status: 'running' as const,
          started_at: new Date().toISOString(),
        }],
      } as any)
    }

    const handleToolResult = (eventData: any) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      const messageId = eventData.message_id || eventData.data?.message_id
      const toolName = eventData.tool_name || eventData.data?.tool_name || 'unknown'
      if (!messageId) return

      const msgs = useSessionStore.getState().messages[sid] || []
      const msg = msgs.find((m) => m.id === messageId)
      const existing = msg?.toolCalls || []
      const callId = eventData.call_id || eventData.data?.call_id

      const updated = existing.map((tc) => {
        if (tc.tool_name === toolName && tc.status === 'running') {
          return {
            ...tc,
            status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
            result: eventData.result ?? eventData.data?.result,
            completed_at: new Date().toISOString(),
            duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
          }
        }
        return tc
      })
      // If no matching running tool found, add a new completed entry
      if (!updated.some((tc) => tc.tool_name === toolName && tc.status !== 'running')) {
        updated.push({
          call_id: callId || `call_${Date.now()}`,
          tool_name: toolName,
          tool_args: {},
          status: (eventData.success ?? eventData.data?.success ?? true) ? 'completed' as const : 'failed' as const,
          result: eventData.result ?? eventData.data?.result,
          completed_at: new Date().toISOString(),
          duration_ms: eventData.duration_ms ?? eventData.data?.duration_ms,
        })
      }
      updateMessageFields(sid, messageId, { toolCalls: updated } as any)
    }

    webSocketService.subscribe(WS_SERVER_EVENTS.TOOL_START, handleToolStart)
    webSocketService.subscribe(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)

    return () => {
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.THINKING_START, handleThinkingStart)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.THINKING_CHUNK, handleThinkingChunk)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.THINKING_END, handleThinkingEnd)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.TOOL_START, handleToolStart)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.TOOL_RESULT, handleToolResult)
    }
  }, [addMessage, updateMessageContent, updateMessageFields, setStreaming])

  // ------------------------------------------
  // 组件卸载时断开 WebSocket
  // ------------------------------------------
  useEffect(() => {
    return () => {
      disconnectWebSocket()
    }
  }, [disconnectWebSocket])

  /**
   * 选择会话
   *
   * 设置活跃会话并建立 WebSocket 连接。
   * setActiveSession 会自动加载历史消息。
   */
  const handleSelectSession = useCallback(
    async (sessionId: string) => {
      await setActiveSession(sessionId)
      const currentToken = useAuthStore.getState().token
      if (currentToken) {
        connectWebSocket(sessionId, currentToken)
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
   * 1. 将用户消息添加到本地状态
   * 2. 通过 WebSocket 发送用户输入
   */
  const handleSendMessage = useCallback(
    async (params: SendMessageParams) => {
      const { activeSessionId: sid } = useSessionStore.getState()
      const currentToken = useAuthStore.getState().token

      if (!sid || !currentToken) return

      // 添加用户消息到本地状态
      const userMessage: Message = {
        id: crypto.randomUUID(),
        sessionId: sid,
        role: 'user',
        content: params.content,
        timestamp: new Date().toISOString(),
        parentId: null,
        sequence: 0,
      }
      addMessage(sid, userMessage)

      // 通过 WebSocket 发送用户输入
      await webSocketService.sendUserInput(
        params.content,
        undefined,
        params.enableThinking,
        undefined,
      )
    },
    [addMessage],
  )

  /**
   * 停止生成
   */
  const handleStopGenerate = useCallback(() => {
    webSocketService.sendCancel()
    stopStreaming()
  }, [stopStreaming])

  /**
   * 编辑消息
   */
  const handleEditMessage = useCallback(
    async (messageId: string, newContent: string) => {
      if (!activeSessionId) return
      await messageActions.editMessage(messageId, newContent)
      // 重新加载消息以获取更新后的内容
      const { fetchMessages } = useSessionStore.getState()
      await fetchMessages(activeSessionId)
    },
    [activeSessionId, messageActions],
  )

  /**
   * 重新生成消息
   */
  const handleRegenerateMessage = useCallback(
    async (messageId: string) => {
      if (!activeSessionId) return
      await messageActions.retryMessageWithScope(messageId, 'all')
    },
    [activeSessionId, messageActions],
  )

  /**
   * 删除消息
   */
  const handleDeleteMessage = useCallback(
    async (messageId: string) => {
      if (!activeSessionId) return
      await messageActions.deleteMessage(messageId)
    },
    [activeSessionId, messageActions],
  )

  /**
   * 登出并跳转到登录页
   */
  const handleLogout = useCallback(async () => {
    disconnectWebSocket()
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
              onClick={() => handleSelectSession(session.id)}
              className={`cursor-pointer truncate px-3 py-2.5 text-sm transition-colors ${
                activeSessionId === session.id
                  ? 'bg-accent text-accent-foreground font-medium'
                  : 'hover:bg-accent/50 text-foreground/80'
              }`}
              title={session.title}
            >
              {session.title}
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
      isGenerating={isStreaming}
      modelName={modelName}
      onSendMessage={handleSendMessage}
      onStopGenerate={handleStopGenerate}
      onEdit={handleEditMessage}
      onRegenerate={handleRegenerateMessage}
      onDelete={handleDeleteMessage}
      hasMoreMessages={pagination?.hasMore ?? false}
      isLoadingMoreMessages={pagination?.isLoadingMore ?? false}
      onLoadMoreMessages={() => {
        if (activeSessionId) loadMoreMessages(activeSessionId)
      }}
      className="flex-1"
    />
  ) : (
    <div className="flex flex-1 flex-col items-center justify-center gap-6 px-8">
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
          className="border-border hover:bg-accent rounded-lg border px-5 py-2.5 text-sm transition-colors"
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
        topNavContent={
          <nav className="flex min-w-0 items-center gap-1 overflow-x-auto">
            {[
              { path: ROUTES.TOOLS, label: '工具' },
              { path: ROUTES.AGENTS, label: '智能体' },
              { path: ROUTES.MONITORING, label: '监控' },
              { path: ROUTES.MEMORY, label: '记忆' },
              { path: ROUTES.SETTINGS, label: '设置' },
              { path: ROUTES.DEBUG.ROOT, label: '调试' },
            ].map((item) => (
              <a
                key={item.path}
                href={item.path}
                className="text-muted-foreground hover:text-foreground hover:bg-accent shrink-0 rounded px-2 py-1 text-xs whitespace-nowrap transition-colors"
              >
                {item.label}
              </a>
            ))}
          </nav>
        }
        onToggleMode={toggleLayoutMode}
      />
    )
  }

  // ---- Classic layout mode (original) ----
  return (
    <div className="bg-background text-foreground flex h-screen flex-col">
      {/* 顶部导航栏 */}
      <header className="flex h-10 shrink-0 items-center justify-between border-b px-3">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setSidebarCollapsed((prev) => !prev)}
            className="hover:bg-accent rounded p-1 transition-colors"
            title={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
          >
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M4 6h16M4 12h16M4 18h16"
              />
            </svg>
          </button>
          <h1 className="text-base font-semibold">超级终端</h1>
          <span
            className={`h-2 w-2 rounded-full ${isWsConnected ? 'bg-green-500' : 'bg-gray-400'}`}
            title={isWsConnected ? 'WebSocket 已连接' : 'WebSocket 未连接'}
          />
        </div>
        <div className="flex items-center gap-3">
          <nav className="mr-2 flex min-w-0 shrink items-center gap-1 overflow-x-auto">
            {[
              { path: ROUTES.TOOLS, label: '工具' },
              { path: ROUTES.AGENTS, label: '智能体' },
              { path: ROUTES.MONITORING, label: '监控' },
              { path: ROUTES.MEMORY, label: '记忆' },
              { path: ROUTES.SETTINGS, label: '设置' },
              { path: ROUTES.DEBUG.ROOT, label: '调试' },
            ].map((item) => (
              <a
                key={item.path}
                href={item.path}
                className="text-muted-foreground hover:text-foreground hover:bg-accent shrink-0 rounded px-2 py-1 text-xs whitespace-nowrap transition-colors"
              >
                {item.label}
              </a>
            ))}
          </nav>
          {/* Layout toggle button */}
          <button
            onClick={toggleLayoutMode}
            className="hover:bg-accent text-muted-foreground flex items-center gap-1 rounded-md px-2 py-1 text-xs transition-colors"
            title="Switch to five-space layout"
          >
            <LayoutGrid className="h-3.5 w-3.5" />
            <span className="hidden sm:inline">Five-space</span>
          </button>
          <span className="text-muted-foreground text-sm">{user?.username}</span>
          <button
            onClick={handleLogout}
            className="text-muted-foreground hover:text-foreground text-sm transition-colors"
          >
            登出
          </button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {/* 左侧会话列表面板 */}
        <aside
          className={`${
            sidebarCollapsed ? 'w-0' : 'w-56'
          } flex shrink-0 flex-col overflow-hidden border-r transition-all duration-200`}
        >
          {sidebarContent}
        </aside>

        {/* 右侧聊天区域 */}
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
