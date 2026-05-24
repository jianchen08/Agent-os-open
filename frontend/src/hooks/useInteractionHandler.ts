/**
 * useInteractionHandler Hook
 *
 * 业务编排层：订阅 WebSocket 交互事件 → 解析数据写入 store → 提供 actions 给 UI。
 * 单一职责：只处理人类交互相关逻辑。
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { ROUTES } from '@/constants/routes'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { registerFileReview } from '@/stores/fileReviewRegistry'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { playNotificationSound } from '@/utils/audioNotification'
import type { PendingInteraction } from '@/stores/interactionStore'

/**
 * 从 WebSocket interaction_request 事件数据解析为 PendingInteraction
 */
function parseInteractionEvent(
  data: Record<string, unknown>,
): Omit<PendingInteraction, 'status'> | null {
  const inner = (data.data as Record<string, unknown>) || data

  const requestId = (inner.request_id as string) || (inner.requestId as string)
  if (!requestId) return null

  const rawAgentLevel = (
    (inner.agent_level as string)
    || (inner.agentLevel as string)
    || ''
  ).toUpperCase()

  // BUG-FIX-fix_20260510_file_contents:
  // 问题根因: parseInteractionEvent 没有解析 file_contents 字段，导致前端收不到文件内容
  // 修复方案: 添加 file_contents / fileContents 字段解析
  const fileContents = (
    (inner.file_contents as Record<string, string>)
    || (inner.fileContents as Record<string, string>)
    || undefined
  )

  const sessionId = (inner.session_id as string) || (inner.sessionId as string) || undefined

  return {
    requestId,
    mode: (inner.interaction_mode as 'choice' | 'conversation' | 'notification') || 'choice',
    title: (inner.title as string) || '',
    description: (inner.description as string) || '',
    threadId: (inner.thread_id as string) || (inner.threadId as string) || '',
    tabId: (inner.tab_id as string) || (inner.tabId as string) || '',
    agentId: (inner.agent_id as string) || (inner.agentId as string) || '',
    pipelineId: (inner.pipeline_id as string) || (inner.pipelineId as string) || '',
    options: inner.options as PendingInteraction['options'],
    questions: inner.questions as string[],
    initialMessage: inner.initial_message as string,
    suggestions: inner.suggestions as string[],
    priority: inner.priority as PendingInteraction['priority'],
    progress: inner.progress as number | undefined,
    timestamp: new Date().toISOString(),
    agentLevel: rawAgentLevel || undefined,
    fileContents,
    sessionId,
  }
}

export function useInteractionHandler(sessionId: string | undefined) {
  const navigate = useNavigate()
  const addInteraction = useInteractionStore((s) => s.addInteraction)
  const markResponded = useInteractionStore((s) => s.markResponded)
  const markNavigated = useInteractionStore((s) => s.markNavigated)
  const markEntered = useInteractionStore((s) => s.markEntered)
  const dismissInteraction = useInteractionStore((s) => s.dismissInteraction)
  const pendingInteractions = useInteractionStore((s) => s.pendingInteractions)

  const scheduledDismissals = useRef<Set<string>>(new Set())

  const sessionPending = useMemo(
    () =>
      sessionId
        ? pendingInteractions.filter((i) => i.status !== 'dismissed')
        : [],
    [pendingInteractions, sessionId],
  )

  useEffect(() => {
    const completed = pendingInteractions.filter(
      (i) => i.status === 'responded' || i.status === 'navigated',
    )
    if (completed.length === 0) return

    for (const item of completed) {
      if (!scheduledDismissals.current.has(item.requestId)) {
        scheduledDismissals.current.add(item.requestId)
        if (item.status === 'navigated') {
          dismissInteraction(item.requestId)
        } else {
          setTimeout(() => {
            scheduledDismissals.current.delete(item.requestId)
            dismissInteraction(item.requestId)
          }, 2000)
        }
      }
    }
  }, [pendingInteractions, dismissInteraction])

  useEffect(() => {
    const requestToNotificationMap = new Map<string, string>()

    const handleInteractionRequest = (data: Record<string, unknown>) => {
      const parsed = parseInteractionEvent(data)
      if (!parsed) return

      // BUG-FIX-fix_20260512_duplicate_interaction:
      // 问题根因: seenRequestIds 在 useEffect 闭包内，React 重渲染时闭包重建，
      //           seenRequestIds 清空，同一请求被重复处理（添加两个卡片 + 两个通知）。
      // 修复方案: 用 interactionStore 检查是否已存在，store 是全局持久化的不会重置。
      const existing = useInteractionStore.getState().pendingInteractions.find(
        (i) => i.requestId === parsed.requestId,
      )
      if (existing) return

      addInteraction(parsed)
      playNotificationSound().catch(() => {})

      const notifId = useNotificationStore.getState().addNotification({
        title: parsed.title || '人类交互请求',
        message: parsed.description || `${parsed.agentId || 'Agent'} 请求您的输入`,
        priority: (parsed.priority as 'high' | 'normal' | 'low') || 'high',
        category: 'alert',
        isBlocking: false,
        sourceId: parsed.requestId,
      })
      requestToNotificationMap.set(parsed.requestId, notifId)

      if (parsed.fileContents && Object.keys(parsed.fileContents).length > 0) {
        const tabId = `review-${parsed.requestId}`
        registerFileReview(tabId, {
          requestId: parsed.requestId,
          mode: parsed.mode as 'choice' | 'conversation' | 'notification',
          title: parsed.title || '',
          pipelineId: parsed.pipelineId || '',
          fileContents: parsed.fileContents,
          options: parsed.options,
          sessionId: parsed.sessionId,
        })
        // BUG-FIX-fix_20260512_tab_not_switching:
        // 问题根因: addWorkspaceTab 只追加 tab 到数组，不将其他 tab 的 isActive 设为 false，
        //          导致多个 tab 同时 isActive=true，WorkspacePanel 用 find 取第一个，
        //          内容区渲染旧 tab 内容而新 tab 样式显示为选中。
        // 修复方案: 添加 tab 后调用 setActiveTab 正确切换活跃标签页。
        const layoutStore = useLayoutModeStore.getState()
        const existingTab = layoutStore.workspaceTabs.find((t) => t.id === tabId)
        if (!existingTab) {
          layoutStore.addWorkspaceTab({
            id: tabId,
            title: parsed.title || '文件审阅',
            icon: '📄',
            moduleId: '__file_review__',
            isActive: true,
            isPinned: false,
          })
        }
        layoutStore.setActiveTab(tabId)
      }
    }

    const removeNotificationForRequest = (requestId: string) => {
      const notifId = requestToNotificationMap.get(requestId)
      if (notifId) {
        useNotificationStore.getState().removeNotification(notifId)
        requestToNotificationMap.delete(requestId)
      }
    }

    const handleInteractionCancelled = (data: Record<string, unknown>) => {
      const requestId = (data.request_id as string) || (data.requestId as string)
      if (requestId) {
        dismissInteraction(requestId)
        removeNotificationForRequest(requestId)
      }
    }

    const handleInteractionTimeout = (data: Record<string, unknown>) => {
      const requestId = (data.request_id as string) || (data.requestId as string)
      if (requestId) {
        dismissInteraction(requestId)
        removeNotificationForRequest(requestId)
      }
    }

    globalWS.subscribe(
      WS_SERVER_EVENTS.INTERACTION_REQUEST,
      handleInteractionRequest as any,
    )
    globalWS.subscribe(
      'interaction_cancelled',
      handleInteractionCancelled as any,
    )
    globalWS.subscribe(
      'interaction_timeout',
      handleInteractionTimeout as any,
    )

    return () => {
      globalWS.unsubscribe(
        WS_SERVER_EVENTS.INTERACTION_REQUEST,
        handleInteractionRequest as any,
      )
      globalWS.unsubscribe(
        'interaction_cancelled',
        handleInteractionCancelled as any,
      )
      globalWS.unsubscribe(
        'interaction_timeout',
        handleInteractionTimeout as any,
      )
    }
  }, [addInteraction, dismissInteraction])

  const removeNotificationBySource = (sourceId: string) => {
    const store = useNotificationStore.getState()
    const notif = store.notifications.find((n) => (n as any).sourceId === sourceId)
    if (notif) {
      store.removeNotification(notif.id)
    }
  }

  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      const sid = useSessionStore.getState().activeSessionId
      console.log('[InteractionHandler] respondChoice | requestId=%s | sid=%s | selectedOption=%s', requestId, sid, selectedOption)
      if (!sid) {
        console.warn('[InteractionHandler] respondChoice 中止: activeSessionId 为空!')
        return
      }
      await globalWS.sendInteractionResponse(sid, requestId, {
        responseType: 'answered',
        selectedOption,
        feedback,
      })
      markResponded(requestId)
      removeNotificationBySource(requestId)
    },
    [markResponded],
  )

  const respondConversation = useCallback(
    async (requestId: string, feedback: string) => {
      const sid = useSessionStore.getState().activeSessionId
      console.log('[InteractionHandler] respondConversation | requestId=%s | sid=%s | feedback=%s', requestId, sid, feedback.slice(0, 50))
      if (!sid) {
        console.warn('[InteractionHandler] respondConversation 中止: activeSessionId 为空!')
        return
      }
      await globalWS.sendInteractionResponse(sid, requestId, {
        responseType: 'answered',
        feedback,
      })
      markResponded(requestId)
      removeNotificationBySource(requestId)
    },
    [markResponded],
  )

  const navigateToTab = useCallback(
    async (requestId: string, threadId: string, title?: string, agentLevelStr?: string, interactionSessionId?: string) => {
      const currentSid = useSessionStore.getState().activeSessionId
      if (!currentSid) return

      // 发送 interaction_response(type=approved) 解除后端阻塞，
      // 后端返回 user_arrived → 管道走 wait 路由挂起 → 等用户发消息再唤醒。
      await globalWS.sendInteractionResponse(currentSid, requestId, {
        responseType: 'approved',
        feedback: '用户已进入对话标签页',
      })

      markEntered(requestId)
      removeNotificationBySource(requestId)

      if (!threadId) return

      // 进入对话后管道挂起等待用户输入，清理当前活跃管道的流式状态以恢复发送按钮
      const pipelineStore = usePipelineMessageStore.getState()
      const activePid = pipelineStore.activePipelineId
      if (activePid && pipelineStore.streamingState[activePid]?.isStreaming) {
        pipelineStore.stopStreaming(activePid)
      }

      if (window.location.pathname !== ROUTES.HOME) {
        navigate(ROUTES.HOME, { replace: true })
      }

      // 解析 agentLevel
      let agentLevel: 1 | 2 | 3 = 2
      if (agentLevelStr) {
        const upper = agentLevelStr.toUpperCase()
        if (upper === 'L1' || upper === '1') agentLevel = 1
        else if (upper === 'L3' || upper === '3') agentLevel = 3
      }

      // 使用全局管道导航服务跳转（自动处理跨会话查找和标签创建）
      await navigateToPipeline(threadId, {
        agentName: title || '对话',
        agentLevel,
      })
    },
    [markEntered, navigate],
  )

  return {
    pendingInteractions: sessionPending,
    respondChoice,
    respondConversation,
    navigateToTab,
  }
}
