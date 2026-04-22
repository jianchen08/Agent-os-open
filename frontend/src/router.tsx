/**
 * 路由配置
 *
 * 定义应用的所有路由，包含登录/注册和受保护的主页面。
 * 主页包含完整的聊天界面：左侧会话列表 + 右侧聊天区域。
 */

import { createBrowserRouter, Navigate, useNavigate } from 'react-router-dom'
import { lazy, Suspense, useEffect, useState, useCallback, useMemo } from 'react'
import type { ReactNode } from 'react'
import { ROUTES } from './constants/routes'
import { WS_SERVER_EVENTS } from './constants/websocket'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { ChatContainer } from './components/chat/ChatContainer'
import { useAuthStore } from './stores/authStore'
import { useSessionStore } from './stores/sessionStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useStreamingStore } from './stores/streamingStore'
import { webSocketService } from './services/websocket/WebSocketService'
import type { Message } from './types/models'
import type { SendMessageParams } from './components/chat/types'

const ModulesSettingsPage = lazy(() =>
  import('@/pages/settings/ModulesSettingsPage').then(m => ({ default: m.ModulesSettingsPage }))
)

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
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="text-center space-y-2">
          <div className="w-8 h-8 border-2 border-primary border-t-transparent rounded-full animate-spin mx-auto" />
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
    getMessagePagination,
    loadMoreMessages,
  } = useSessionStore()
  const { fetchSessions, createSession, setActiveSession, deleteSession } = useSessionListStore()
  const { isStreaming, setStreaming, stopStreaming } = useStreamingStore()

  /** 侧边栏是否折叠 */
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)

  /** 当前活跃会话的消息列表（响应式） */
  const activeMessages = useMemo(
    () => (activeSessionId ? messages[activeSessionId] || [] : []),
    [activeSessionId, messages]
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

    return () => {
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.NEW_MESSAGE, handleNewMessage)
    }
  }, [addMessage, updateMessageContent, setStreaming])

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
    [setActiveSession, connectWebSocket]
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
        undefined
      )
    },
    [addMessage]
  )

  /**
   * 停止生成
   */
  const handleStopGenerate = useCallback(() => {
    webSocketService.sendCancel()
    stopStreaming()
  }, [stopStreaming])

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

  return (
    <div className="h-screen flex flex-col bg-background text-foreground">
      {/* 顶部导航栏 */}
      <header className="h-12 border-b flex items-center justify-between px-4 shrink-0">
        <div className="flex items-center gap-2">
          <button
            onClick={() => setSidebarCollapsed(prev => !prev)}
            className="p-1 hover:bg-accent rounded transition-colors"
            title={sidebarCollapsed ? '展开侧边栏' : '折叠侧边栏'}
          >
            <svg
              className="w-5 h-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
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
            className={`w-2 h-2 rounded-full ${
              isWsConnected ? 'bg-green-500' : 'bg-gray-400'
            }`}
            title={isWsConnected ? 'WebSocket 已连接' : 'WebSocket 未连接'}
          />
        </div>
        <div className="flex items-center gap-3">
          <span className="text-sm text-muted-foreground">{user?.username}</span>
          <button
            onClick={handleLogout}
            className="text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            登出
          </button>
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        {/* 左侧会话列表面板 */}
        <aside
          className={`${
            sidebarCollapsed ? 'w-0' : 'w-64'
          } border-r flex flex-col shrink-0 transition-all duration-200 overflow-hidden`}
        >
          {/* 新建会话按钮 */}
          <div className="p-3 border-b shrink-0">
            <button
              onClick={handleCreateSession}
              className="w-full px-3 py-2 text-sm bg-primary text-primary-foreground rounded-lg hover:opacity-90 transition-opacity"
            >
              + 新会话
            </button>
          </div>

          {/* 会话列表 */}
          <div className="flex-1 overflow-y-auto">
            {sessions.length === 0 ? (
              <div className="p-4 text-center text-sm text-muted-foreground">
                暂无会话
              </div>
            ) : (
              sessions.map(session => (
                <div
                  key={session.id}
                  onClick={() => handleSelectSession(session.id)}
                  className={`px-3 py-2.5 cursor-pointer text-sm truncate transition-colors ${
                    activeSessionId === session.id
                      ? 'bg-accent text-accent-foreground'
                      : 'hover:bg-accent/50'
                  }`}
                  title={session.title}
                >
                  {session.title}
                </div>
              ))
            )}
          </div>
        </aside>

        {/* 右侧聊天区域 */}
        <main className="flex-1 flex flex-col overflow-hidden">
          {activeSessionId ? (
            <ChatContainer
              sessionId={activeSessionId}
              messages={activeMessages}
              isLoading={isSessionLoading}
              isGenerating={isStreaming}
              onSendMessage={handleSendMessage}
              onStopGenerate={handleStopGenerate}
              hasMoreMessages={pagination?.hasMore ?? false}
              isLoadingMoreMessages={pagination?.isLoadingMore ?? false}
              onLoadMoreMessages={() => {
                if (activeSessionId) loadMoreMessages(activeSessionId)
              }}
              className="flex-1"
            />
          ) : (
            /* 无活跃会话时显示欢迎页 */
            <div className="flex-1 flex items-center justify-center">
              <div className="text-center space-y-4">
                <h2 className="text-2xl font-semibold">欢迎使用超级终端</h2>
                <p className="text-muted-foreground">
                  点击左侧「新会话」开始对话
                </p>
              </div>
            </div>
          )}
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
 * - /settings/modules : 模块设置页（懒加载）
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
      path: '/settings/modules',
      element: (
        <Suspense fallback={<div className="p-4 text-muted-foreground">加载中...</div>}>
          <ModulesSettingsPage />
        </Suspense>
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
