/** 路由配置 定义应用的所有路由，包含登录/注册和受保护的主页面。 */

import { lazy, Suspense, useEffect, useCallback } from 'react'
import { createBrowserRouter, Navigate, useNavigate } from 'react-router-dom'
import { GlobalInteractionOverlay } from './components/chat/GlobalInteractionOverlay'
import { SchemaFullscreenHost } from './components/schema/SchemaFullscreenHost'
import { ChatPanelShell } from './components/layout/ChatPanelShell'
import { Sidebar } from './components/layout/Sidebar'
import { ROUTES } from './constants/routes'
import { useConnectionStatus } from './hooks/useConnectionStatus'
import { useRealtimeEvents } from './hooks/useRealtimeEvents'
import { useWidgetEvents } from './hooks/useWidgetEvents'
import { useTaskPolling } from './hooks/useTaskPolling'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { globalWS } from './services/websocket/GlobalWebSocket'
import { openWorkspacePanelByPath } from './services/workspacePanelOpener'
import { initStreamingEvents, destroyStreamingEvents } from './services/websocket/streamingEventService'
import { flushStreamChunkBuffer } from './services/websocket/streaming/handlers/streamHandler'
import { allocateNextSequence, ensureStreamingPlaceholder } from './services/websocket/streaming/handlers/utils'
import { useAgentStore } from './stores/agentStore'
import { useAgentTabStore } from './stores/agentTabStore'
import { useAuthStore } from './stores/authStore'
import { useInteractionStore } from './stores/interactionStore'
import { usePipelineMessageStore } from './stores/pipelineMessageStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useSessionStore } from './stores/sessionStore'
import { useUIStore } from './stores/uiStore'
import { generateUUID } from './utils/uuid'
import type { SendMessageParams } from './components/chat/types'
import type { Message } from './types'
import type { ReactNode } from 'react'

const SettingsPage = lazy(() =>
  import('@/pages/settings/SettingsPage').then((m) => ({ default: m.SettingsPage })),
)
// 聊天容器懒加载：ChatContainer 依赖链包含 @lobehub/ui 全量入口（EmojiPicker→
// @emoji-mart/data 3.2MB、Markdown→highlight.js 197 语言）与 react-syntax-highlighter
// 全量 Prism（300 语言）、mermaid，静态导入会让 /login 等公共页也必须加载整个聊天
// 依赖链（4000+ 模块），冷启动连接风暴导致页面白屏/加载超时（"前端进不去"）。
// 懒加载后仅进入聊天界面时才拉取该 chunk，登录/注册页首屏只加载轻量依赖。
const ChatContainer = lazy(() =>
  import('./components/chat/ChatContainer').then((m) => ({ default: m.ChatContainer })),
)
const LlmSettingsPage = lazy(() =>
  import('@/pages/settings/LlmSettingsPage').then((m) => ({ default: m.LlmSettingsPage })),
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
const DbAdminPage = lazy(() =>
  import('@/pages/debug/DbAdminPage').then((m) => ({ default: m.DbAdminPage })),
)
const DebugLlmPayloadPage = lazy(() =>
  import('@/pages/debug/DebugLlmPayloadPage').then((m) => ({
    default: m.DebugLlmPayloadPage,
  })),
)
const PluginsSettingsPage = lazy(() =>
  import('@/pages/settings/PluginsSettingsPage').then((m) => ({
    default: m.PluginsSettingsPage,
  })),
)

const ThemeSettingsPage = lazy(() =>
  import('@/pages/settings/ThemeSettingsPage').then((m) => ({
    default: m.ThemeSettingsPage,
  })),
)
const PipelineSettingsPage = lazy(() =>
  import('@/pages/settings/PipelineSettingsPage').then((m) => ({
    default: m.PipelineSettingsPage,
  })),
)
const KnowledgeBasePage = lazy(() =>
  import('@/pages/knowledge-base/KnowledgeBasePage').then((m) => ({
    default: m.KnowledgeBasePage,
  })),
)
const PluginConfigRoute = lazy(() =>
  import('@/pages/settings/PluginConfigRoute').then((m) => ({
    default: m.PluginConfigRoute,
  })),
)
// 插件 page 独立路由渲染器：通配 /p/:pageId → contributionRegistry.getPage → renderPageContent
// （react-router 路由动态化；让插件 page 成为真实 URL 路由，可分享/刷新）
const PluginPageRenderer = lazy(() =>
  import('@/components/schema/PluginPageRenderer').then((m) => ({ default: m.PluginPageRenderer })),
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

  // 开发/本地模式：直接放行，不跳登录页（便于查看布局效果）
  // 生产模式仍走正常鉴权。
  // TODO: 登录入口改为侧边栏（VS Code 式）
  const devBypass = import.meta.env.DEV

  if (!devBypass && isInitializing) {
    return (
      <div className="bg-background text-foreground flex min-h-screen items-center justify-center">
        <div className="space-y-2 text-center">
          <div className="border-primary mx-auto h-8 w-8 animate-spin rounded-full border-2 border-t-transparent" />
          <p className="text-muted-foreground text-sm">加载中...</p>
        </div>
      </div>
    )
  }

  if (!devBypass && !isAuthenticated) {
    return <Navigate to={ROUTES.LOGIN} replace />
  }

  return (
    <>
      {children}
      {/* 全局交互浮层：在所有受保护页面中显示待处理交互 */}
      <GlobalInteractionOverlay />
      {/* 全屏声明浮层：订阅插件 ui_schema 声明的事件（on_event:*），渲染 fullscreen 空间 widget */}
      <SchemaFullscreenHost />
    </>
  )
}

// 聊天主页

/** 聊天主页组件 登录后的主界面，包含： */
function HomePage(): ReactNode {
  const navigate = useNavigate()
  const { logout } = useAuthStore()

  // Phase 1 hooks: connection status and real-time events
  useConnectionStatus()
  useRealtimeEvents()
  // widget_event 全局订阅（内核 PluginWidgetBroadcaster 推送 + 插件 widget 交互）
  useWidgetEvents()

  // 轮询长期任务状态，作为 WebSocket 断连时的 fallback
  useTaskPolling()

  // 统一 VS Code 壳（无 classic / five-space 双模式）

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const isSessionLoading = useSessionStore((s) => s.isLoading)
  const connectWebSocket = useSessionStore((s) => s.connectWebSocket)
  const disconnectWebSocket = useSessionStore((s) => s.disconnectWebSocket)
  const createSession = useSessionListStore((s) => s.createSession)
  const setActiveSession = useSessionListStore((s) => s.setActiveSession)
  const fetchSessions = useSessionListStore((s) => s.fetchSessions)

  /** 确保 Agent 配置列表已加载（ChatContainer 按 activeTab.agentId 解析当前管道模型） */
  const fetchAgents = useAgentStore((s) => s.fetchAgents)

  useEffect(() => {
    fetchAgents().catch(() => {})
  }, [fetchAgents])

  /** 当前活跃会话的消息列表（从 pipelineMessageStore 响应式读取） */
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)

  /** 排队优先级（ADR-2026-08-15）：切换选中会话时上报内核——全局并发闸门
   * 有排队时，当前选中管道的 run 优先获得槽位（其他管道的排队不饿死，
   * 空槽时照常执行）。 */
  useEffect(() => {
    if (activeSessionId) {
      globalWS.sendActiveThread(activeSessionId, activePipelineId ?? undefined)
    }
  }, [activeSessionId, activePipelineId])

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

  // 响应式订阅 token：登录/登出/token 刷新时自动（重）连 WS。
  // 各类 App 记录登录状态的通用模式：持久化凭证恢复 → 设置响应式状态 → 依赖它的逻辑
  // 自动响应。这里订阅 token 变化，登录后 effect 自动重跑建立 WS 连接。
  const authToken = useAuthStore((s) => s.token)
  useEffect(() => {
    if (authToken) {
      globalWS.connect(authToken)
      useSessionStore.setState({ wsStatus: globalWS.status })
    }
    // authToken 变化（登录获得新 token / 登出清空）时自动重连或断开。
    // globalWS.connect 内部已有幂等保护（相同 token + connected 状态直接 return），
    // 不会重复连接；登出时 authToken 变 null，connect 不被调用（登出逻辑里已有 disconnect）。
  }, [authToken])

  // _status 订阅独立 effect：只注册一次，避免随 token 变化反复订阅/取消
  useEffect(() => {
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
        connectWebSocket(currentToken)
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

      // 管道 ID 是会话的唯一路由键。源头取值：直接从当前会话的真实 pipelineIds[0] 取，
      // 而非依赖 ChatContainer 闭包传入的 params.pipelineId（它来自 activeTab.pipelineRunId，
      // 新建/切换会话后 React 渲染时序可能导致闭包持有旧 tab → 串到旧会话的 pipeline）。
      // sessionStore 是唯一真相源，这里实时读取，彻底杜绝 activePipelineId 滞留旧值。
      const sessionForPid = sessions.find((s) => s.id === sid)
      const targetPipelineId = sessionForPid?.pipelineIds?.[0] || params.pipelineId
      if (!targetPipelineId) {
        console.warn('[handleSendMessage] pipelineId 缺失，终止发送: sid=%s', sid)
        return
      }
      // 校验：params.pipelineId 与会话真实主管道不一致时，说明前端 tab 状态滞后，
      // 记录告警（用真实值发送，避免串桶）。
      if (params.pipelineId && params.pipelineId !== targetPipelineId) {
        console.warn(
          '[handleSendMessage] pipelineId 不一致，用会话真实值: sid=%s param=%s real=%s',
          sid.slice(0, 12),
          params.pipelineId.slice(0, 12),
          targetPipelineId.slice(0, 12),
        )
      }

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
        attachments: params.attachments?.map((att) => ({
          id: att.id,
          name: att.name,
          type: att.type,
          mime_type: att.type,
          url: att.url || '',
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
    const ps = usePipelineMessageStore.getState()
    const currentPipelineId = ps.activePipelineId
    if (sid) {
      globalWS.sendCancel(sid, undefined, currentPipelineId || undefined)
    }
    // 始终刷写缓冲并清理流式状态。即使 activePipelineId 为 null（如点 Stop 时
    // pipeline 尚未激活），也要兜底清理任意残留 streamingState，否则 Stop 按钮
    // 持续显示、下一条新消息会卡在"思考中"。
    flushStreamChunkBuffer()
    if (currentPipelineId) {
      ps.stopStreaming(currentPipelineId)
    } else {
      Object.keys(ps.streamingState).forEach((pid) => ps.stopStreaming(pid))
    }

    // 不再设 5s watchdog：主管道 pipeline_id 会被下一轮对话复用，固定延时的
    // watchdog 会把"停止后 5s 内新发的下一轮流式"误判为残留并强制 stopStreaming，
    // 导致下一轮回复空气泡（内容已持久化但实时渲染被中途杀死）。
    // 真正的 stream_end 丢失由更上层的 keepalive 看门狗（~180s）兜底，此处不重复。
  }, [])

  /** 登出并跳转到登录页 */
  const handleLogout = useCallback(async () => {
    destroyStreamingEvents()
    disconnectWebSocket()
    globalWS.disconnect()
    await logout()
    navigate(ROUTES.LOGIN)
  }, [logout, navigate, disconnectWebSocket])

  // 统一使用 Sidebar 组件（VS Code 风格导航 + 会话列表）
  const sidebarContent = <Sidebar />

  // Render chat content (shared between layouts)
  // ChatContainer 为懒加载组件（见顶部 lazy 定义），Suspense 包裹提供加载占位，
  // 避免聊天 chunk 拉取期间整页空白。
  const chatContent = activeSessionId ? (
    <Suspense fallback={LazyFallback}>
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
    </Suspense>
  ) : (
    <div className="text-foreground flex flex-1 flex-col items-center justify-center gap-8 px-8">
      <div className="flex flex-col items-center gap-4">
        <div className="bg-primary/10 text-primary flex h-20 w-20 items-center justify-center rounded-xl">
          <svg className="h-10 w-10" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" />
          </svg>
        </div>
        <div className="flex flex-col items-center gap-2 text-center">
          <h2 className="text-3xl font-bold tracking-tight">欢迎使用超级终端</h2>
          <p className="text-muted-foreground max-w-md text-base">
            选择左侧已有会话，或创建新会话开始对话
          </p>
        </div>
      </div>
      <div className="grid w-full max-w-xl grid-cols-1 gap-4 sm:grid-cols-2">
        <button
          onClick={handleCreateSession}
          className="bg-card border-border hover:border-primary hover:shadow-md group flex items-start gap-4 rounded-xl border p-5 text-left transition-all"
        >
          <span className="bg-primary text-primary-foreground flex h-10 w-10 shrink-0 items-center justify-center rounded-lg text-xl font-medium">
            +
          </span>
          <span className="flex flex-col gap-1">
            <span className="text-base font-medium">新会话</span>
            <span className="text-muted-foreground text-sm">直接开始一段新的对话</span>
          </span>
        </button>
        <button
          onClick={() => openWorkspacePanelByPath('/agents')}
          className="bg-card border-border hover:border-primary hover:shadow-md group flex items-start gap-4 rounded-xl border p-5 text-left transition-all"
        >
          <span className="bg-primary/10 text-primary flex h-10 w-10 shrink-0 items-center justify-center rounded-lg">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <circle cx="9" cy="8" r="3.25" strokeWidth={1.5} />
              <path strokeLinecap="round" strokeWidth={1.5} d="M3.5 19c.8-2.8 2.9-4.25 5.5-4.25s4.7 1.45 5.5 4.25" />
              <circle cx="16.5" cy="9" r="2.5" strokeWidth={1.5} />
              <path strokeLinecap="round" strokeWidth={1.5} d="M15.2 14.7c2.3.3 4 1.7 4.8 4.3" />
            </svg>
          </span>
          <span className="flex flex-col gap-1">
            <span className="text-base font-medium">浏览智能体</span>
            <span className="text-muted-foreground text-sm">按场景挑选合适的专家角色</span>
          </span>
        </button>
      </div>
    </div>
  )

  // 统一 VS Code 能力布局
  return (
    <ChatPanelShell
      chatContent={chatContent}
      sidebarContent={sidebarContent}
      onLogout={handleLogout}
    />
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
      path: '/settings/plugin/:pluginId/:fileId',
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <PluginConfigRoute />
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
      path: ROUTES.DEBUG.DB,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DbAdminPage />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: ROUTES.DEBUG.LLM_PAYLOAD,
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <DebugLlmPayloadPage />
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
    // 插件 page 独立路由（通配）：插件 page 声明 path 为 /p/<pageId> 即可命中。
    // 不需要运行时动态加路由——一条通配覆盖所有声明了 path 的插件 page。
    // 插在 '*' 兜底之前，确保插件 page 被匹配而非跳转 HOME。
    {
      path: '/p/:pageId',
      element: (
        <ProtectedRoute>
          <Suspense fallback={LazyFallback}>
            <PluginPageRenderer />
          </Suspense>
        </ProtectedRoute>
      ),
    },
    {
      path: '*',
      element: <Navigate to={ROUTES.HOME} replace />,
    },
  ])
}
