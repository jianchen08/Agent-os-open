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
import { useAgentTabStore } from '@/stores/agentTabStore'
import { registerFileReview } from '@/stores/fileReviewRegistry'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useStreamingStore } from '@/stores/streamingStore'
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
      if (!sid) return
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
      if (!sid) return
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

      // BUG-FIX-fix_20260512_conversation_enter_no_response:
      // 问题根因: 点击"进入对话"时只做前端状态变更，不发送 interaction_response，
      //          后端 wait_for_choice 永久阻塞，管道无法挂起等待用户消息。
      // 修复方案: 在跳转前发送 interaction_response(type=approved) 解除后端阻塞，
      //          后端返回 user_arrived → 管道走 wait 路由挂起 → 等用户发消息再唤醒。
      await globalWS.sendInteractionResponse(currentSid, requestId, {
        responseType: 'approved',
        feedback: '用户已进入对话标签页',
      })

      markEntered(requestId)
      removeNotificationBySource(requestId)

      if (!threadId) return

      // 进入对话后管道挂起等待用户输入，清除 streaming 状态恢复发送按钮
      const streamingStore = useStreamingStore.getState()
      streamingStore.stopStreamingForTab(threadId)
      streamingStore.stopStreamingForTab(currentSid)

      const tabStore = useAgentTabStore.getState()
      const activeTab = tabStore.tabs.find((t) => t.id === tabStore.activeTabId)

      const isAlreadyThere = activeTab && (
        (activeTab.agentLevel === 1 && threadId === currentSid)
        || activeTab.pipelineRunId === threadId
        || activeTab.parentRecordId === threadId
      )
      if (isAlreadyThere) return

      if (window.location.pathname !== ROUTES.HOME) {
        navigate(ROUTES.HOME, { replace: true })
      }

      const isMainPipeline = threadId === currentSid
      if (isMainPipeline) {
        const mainTab = tabStore.tabs.find((t) => t.agentLevel === 1)
        if (mainTab) tabStore.switchToTab(mainTab.id)
        return
      }

      const findByPipeline = tabStore.getTabIdByPipeline(threadId)
      const findByRunId = tabStore.tabs.find((t) => t.pipelineRunId === threadId)
      const findByParent = tabStore.tabs.find((t) => t.parentRecordId === threadId && t.agentLevel !== 1)

      const targetTab =
        (findByPipeline && tabStore.tabs.some((t) => t.id === findByPipeline)) ? findByPipeline
        : findByRunId ? findByRunId.id
        : findByParent ? findByParent.id
        : null

      if (targetTab) {
        tabStore.switchToTab(targetTab)
      } else {
        const newTabId = `sub-${threadId}`
        tabStore.openSubAgentTab({
          agentId: threadId,
          agentName: title || '对话',
          parentRecordId: threadId,
          agentLevel: 2,
          status: 'running',
          setActive: true,
          pipelineId: threadId,
        })
        tabStore.loadTabMessages(newTabId, threadId)
      }
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
