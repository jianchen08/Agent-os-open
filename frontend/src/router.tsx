/** 路由配置 定义应用的所有路由，包含登录/注册和受保护的主页面。 */

import { lazy, Suspense, useEffect, useCallback } from 'react'
import { createBrowserRouter, Navigate, useNavigate } from 'react-router-dom'
import { GlobalInteractionOverlay } from './components/chat/GlobalInteractionOverlay'
import { ChatPanelShell } from './components/layout/ChatPanelShell'
import { Sidebar } from './components/layout/Sidebar'
import { SchemaFullscreenHost } from './components/schema/SchemaFullscreenHost'
import { ROUTES } from './constants/routes'
import { useAgentsQuery } from './hooks/queries/useAgentsQuery'
import { useLongTermTasksQuery } from './hooks/queries/useLongTermTasksQuery'
import { useSessionsQuery, readSessions } from './hooks/queries/useSessionsQuery'
import { useConnectionStatus } from './hooks/useConnectionStatus'
import { useRealtimeEvents } from './hooks/useRealtimeEvents'
import { useWidgetEvents } from './hooks/useWidgetEvents'
import { LoginPage } from './pages/auth/LoginPage'
import { RegisterPage } from './pages/auth/RegisterPage'
import { globalWS } from './services/websocket/GlobalWebSocket'
import { loadSessionExecutionOptions } from './services/sessionExecutionOptions'
import { flushStreamChunkBuffer } from './services/websocket/streaming/handlers/streamHandler'
import { initStreamingEvents, destroyStreamingEvents } from './services/websocket/streamingEventService'
import { openWorkspacePanelByPath } from './services/workspacePanelOpener'
import { useAgentTabStore } from './stores/agentTabStore'
import { useAuthStore } from './stores/authStore'
import { useInteractionStore } from './stores/interactionStore'
import { useNotificationStore } from './stores/notificationStore'
import { usePendingInputStore } from './stores/pendingInputStore'
import { usePipelineMessageStore } from './stores/pipelineMessageStore'
import { useSessionListStore } from './stores/sessionListStore'
import { useSessionStore } from './stores/sessionStore'
import { useUIStore } from './stores/uiStore'
import { appendAttachmentRefs } from './utils/attachmentRefs'
import { resolveSendTarget } from './utils/mappers'
import { generateUUID } from './utils/uuid'
import type { SendMessageParams } from './components/chat/types'
import type { ReactNode } from 'react'

// 设置承载：唯一 UI = SettingsHubWidget（settings_hub）工作区页签，无独立路由页。
// 聊天容器懒加载：ChatContainer 依赖链包含 @lobehub/ui 全量入口（EmojiPicker→
// @emoji-mart/data 3.2MB、Markdown→highlight.js 197 语言）与 react-syntax-highlighter
// 全量 Prism（300 语言）、mermaid，静态导入会让 /login 等公共页也必须加载整个聊天
// 依赖链（4000+ 模块），冷启动连接风暴导致页面白屏/加载超时（"前端进不去"）。
// 懒加载后仅进入聊天界面时才拉取该 chunk，登录/注册页首屏只加载轻量依赖。
const ChatContainer = lazy(() =>
  import('./components/chat/ChatContainer').then((m) => ({ default: m.ChatContainer })),
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
const KnowledgeBasePage = lazy(() =>
  import('@/pages/knowledge-base/KnowledgeBasePage').then((m) => ({
    default: m.KnowledgeBasePage,
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

  // 长期任务列表：5s 兜底轮询由 useLongTermTasksQuery 的 refetchInterval
  // 承担（页面隐藏自动暂停）；WS 断连期间任务状态变化靠此兜底对账恢复
  useLongTermTasksQuery()

  // 统一 VS Code 壳（无 classic / five-space 双模式）

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const connectWebSocket = useSessionStore((s) => s.connectWebSocket)
  const disconnectWebSocket = useSessionStore((s) => s.disconnectWebSocket)
  const createSession = useSessionListStore((s) => s.createSession)
  const setActiveSession = useSessionListStore((s) => s.setActiveSession)
  const restoreActiveSessionIfNeeded = useSessionListStore((s) => s.restoreActiveSessionIfNeeded)

  // 会话列表（query 化）：缓存秒开 + staleTime 到期后台静默刷新，刷新页面不再
  // 全屏 loading；首次无缓存的 isPending 才驱动 ChatContainer 的加载态
  const sessionsQuery = useSessionsQuery()
  const isSessionLoading = sessionsQuery.isPending

  // Agent 配置列表（query 化）：ChatContainer 按 activeTab.agentId 解析当前管道模型，
  // 订阅即加载（缓存命中时零请求）
  useAgentsQuery()

  // query 数据到位后恢复上次选中会话（内部幂等：已有有效选中时不动作）
  useEffect(() => {
    if (sessionsQuery.data) {
      restoreActiveSessionIfNeeded(sessionsQuery.data).catch(console.error)
    }
  }, [sessionsQuery.data, restoreActiveSessionIfNeeded])

  /** 当前活跃会话的消息列表（从 pipelineMessageStore 响应式读取） */
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)

  /** 排队优先级（[来源: docs/decisions/2026-08-15-pipeline-run-chain-serialization.md]）：
   * 切换选中会话时上报内核——全局并发闸门
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
      useSessionStore.setState({ wsStatus: data.status })
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
      // apiClient 拦截器构造的是普通 ApiError（非 Error 实例），直接读 .message
      useNotificationStore.getState().addNotification({
        title: '创建会话失败',
        message: (error as { message?: string })?.message ?? '请检查网络连接后重试',
        priority: 'high',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 8000,
        sourceLabel: '前端',
      })
    }
  }, [createSession, handleSelectSession])

  /** 发送消息：目标 = 所在标签管道（主/子一对一，成员校验不过 fail-closed），乐观消息与流式态写 pipelineMessageStore 对应管道桶 */
  const handleSendMessage = useCallback(
    async (params: SendMessageParams) => {
      const { activeSessionId: sid } = useSessionStore.getState()
      const currentToken = useAuthStore.getState().token

      if (!sid || !currentToken) {
        return
      }

      const listStore = useSessionListStore.getState()
      const sessions = readSessions()
      const session = sessions.find(s => s.id === sid)
      if (session && (session.title === '灵汐' || session.title === '新会话')) {
        const title = params.content.replace(/\n/g, ' ').trim().slice(0, 30)
        if (title) {
          listStore.renameSession(sid, title)
        }
      }

      const pipelineStore = usePipelineMessageStore.getState()

      // 管道路由（一对一）：目标 = 发送所在标签的管道 params.pipelineId
      // （主标签=主管道，子标签=子管道），原样透传不做主管道改写。会话串桶
      // 由成员校验拦截：目标不属于当前会话（标签管道映射 ∪ 会话快照
      // pipelineIds，两源都按 sid 划界）即状态滞后/脏值，fail-closed 终止
      // 发送并显式通知，绝不静默改发主管道（子管道视图发送落主管道 = 写错
      // 桶，与内核 resolve_pipeline_id_for_thread 同一裁定）。
      const targetPipelineId = resolveSendTarget(
        params.pipelineId,
        session,
        useAgentTabStore.getState().pipelineTabMap,
      )
      if (!targetPipelineId) {
        console.warn(
          '[handleSendMessage] 目标管道不属于当前会话，终止发送: sid=%s pid=%s',
          sid.slice(0, 12),
          params.pipelineId?.slice(0, 12) ?? '(empty)',
        )
        useNotificationStore.getState().addNotification({
          title: '发送已终止',
          message: '消息目标管道不属于当前会话，为防串桶已阻止发送，请刷新页面后重试',
          priority: 'high',
          category: 'error',
          isBlocking: false,
          autoDismissMs: 8000,
          sourceLabel: '前端',
        })
        return
      }

      const userMessageId = generateUUID()
      // 附件索引随 content 携带（[来源: docs/decisions/2026-08-21-multimodal-attachments-chain.md]）：
      // markdown 引用并入正文——
      // 内核零改动（照旧只存文本 content），multimodal_preprocessor 识别
      // /uploads/ 引用、llm_core 发送前读文件转 base64；用户消息气泡 markdown
      // 渲染图片/链接，历史回读天然带引用。不再挂 attachments 数组（避免与
      // markdown 图片双重显示）。
      const contentWithRefs = appendAttachmentRefs(params.content, params.attachments)

      // [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md] 单一消息数组：
      // 乐观 user、流式 assistant 全在 messagesByPipeline 同一数组，靠 status
      // 状态机区分生命周期；独立 pending 区不存在。new_message 事件携带
      // user_message 权威回传时按 cmid 认领（recordId 双字段范式，UI id 永不变）。
      // ── busy 分支（ADR-2026-08-26）──
      // 管道执行中（streaming）发送 → 消息照常走 WS（内核 pending 队列排队，
      // 等待窗口内可编辑/删除/清空），但不建乐观气泡/不启动流式态——
      // 队列条由 pending_inputs_changed 事件同步；消费激活时 stream_start
      // 到达 → 现有流式协议接管（占位气泡 → 认领 → 回复）。
      if (pipelineStore.isStreaming(targetPipelineId)) {
        globalWS.sendUserInput(sid, contentWithRefs, {
          enableThinking: params.enableThinking,
          pipelineId: targetPipelineId,
          clientMessageId: userMessageId,
          executionContext: loadSessionExecutionOptions(sid)?.executionContext,
        })
        usePendingInputStore.getState().load(targetPipelineId)
        return
      }
      pipelineStore.addMessage(targetPipelineId, {
        id: userMessageId,
        sessionId: sid,
        role: 'user',
        content: contentWithRefs,
        timestamp: new Date().toISOString(),
        status: 'sending',
        clientMessageId: userMessageId,
      })
      // 发送瞬间启动流式态（驱动"思考中"指示）；stream_start 到达时会以
      // 后端真实 message_id 重建流式态并建 assistant 占位气泡。
      pipelineStore.startStreaming(targetPipelineId, userMessageId)
      // 用户发消息 = 对挂起中 conversation 交互的响应（2026-09-02 裁定）：
      // 先解除挂起（提交空 approved，交互工具返回空回复），消息再推进下一步。
      // 覆盖 pending（未点"进入对话"）与 entered 两态；choice 模式需显式
      // 选选项不自动解除。只按目标管道探测一次：sid 是会话坐标不是管道坐标，
      // 二次探测会把"另一个会话的交互"自动批准掉（store 层按管道精确归属）。
      const pendingConversations =
        useInteractionStore.getState().getPendingConversationsForPipeline(targetPipelineId)
      for (const interaction of pendingConversations) {
        globalWS.sendInteractionResponse(sid, interaction.requestId, {
          response_type: 'approved',
          feedback: '',
        })
        useInteractionStore.getState().markResponded(interaction.requestId)
      }

      // globalWS.sendUserInput 是同步入队（_send 永不抛异常：已连接则 ws.send，
      // 否则入队待重连），发送失败由 user_input_send_timeout（20s TTL）显式
      // 撤下 pending + 插入错误气泡 + 高优通知兜底（诚实状态机，无静默容忍）。
      globalWS.sendUserInput(sid, contentWithRefs, {
        enableThinking: params.enableThinking,
        pipelineId: targetPipelineId,
        clientMessageId: userMessageId,
        executionContext: loadSessionExecutionOptions(sid)?.executionContext,
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

  /** 重新生成：截断到最后一条 user 消息后重跑。本地乐观截断 + WS regenerate。
   *  后端未就绪时（批次 D 内核侧）WS 消息被忽略，本地截断仍即时生效。 */
  const handleRegenerate = useCallback(() => {
    const sid = useSessionStore.getState().activeSessionId
    const ps = usePipelineMessageStore.getState()
    const pipelineId = ps.activePipelineId
    if (!sid || !pipelineId) return
    const lastUserId = ps.findLastUserMessageId(pipelineId)
    if (!lastUserId) return
    ps.truncateMessagesAfter(pipelineId, lastUserId)
    globalWS.sendRegenerate(sid, { pipelineId })
  }, [])

  /** 回退到指定 user 消息（该消息之后整体截断重跑） */
  const handleRollbackTo = useCallback((userMessageId: string) => {
    const sid = useSessionStore.getState().activeSessionId
    const ps = usePipelineMessageStore.getState()
    const pipelineId = ps.activePipelineId
    if (!sid || !pipelineId) return
    ps.truncateMessagesAfter(pipelineId, userMessageId)
    globalWS.sendRegenerate(sid, { pipelineId, userMessageId })
  }, [])

  /** 编辑重发：改写目标 user 消息内容并截断其后重跑 */
  const handleEditResend = useCallback(async (messageId: string, newContent: string) => {
    const sid = useSessionStore.getState().activeSessionId
    const ps = usePipelineMessageStore.getState()
    const pipelineId = ps.activePipelineId
    if (!sid || !pipelineId) return
    ps.truncateMessagesAfter(pipelineId, messageId)
    ps.updateMessage(pipelineId, messageId, { content: newContent })
    globalWS.sendRegenerate(sid, { pipelineId, userMessageId: messageId, newContent })
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
        onSendMessage={handleSendMessage}
        onStopGenerate={handleStopGenerate}
        onRegenerate={handleRegenerate}
        onRollbackTo={handleRollbackTo}
        onEdit={handleEditResend}
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
