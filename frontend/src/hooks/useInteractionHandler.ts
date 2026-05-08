/**
 * useInteractionHandler Hook
 *
 * 业务编排层：订阅 WebSocket 交互事件 → 解析数据写入 store → 提供 actions 给 UI。
 * 单一职责：只处理人类交互相关逻辑。
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { wsPool } from '@/services/websocket/WebSocketConnectionPool'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useAuthStore } from '@/stores/authStore'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import type { PendingInteraction } from '@/stores/interactionStore'
import { playNotificationSound } from '@/utils/audioNotification'

/**
 * 从 WebSocket interaction_request 事件数据解析为 PendingInteraction
 */
function parseInteractionEvent(
  data: Record<string, unknown>,
): Omit<PendingInteraction, 'status'> | null {
  const inner = (data.data as Record<string, unknown>) || data

  const requestId = (inner.request_id as string) || (inner.requestId as string)
  if (!requestId) return null

  return {
    requestId,
    mode: (inner.interaction_mode as 'choice' | 'conversation') || 'choice',
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
    timestamp: new Date().toISOString(),
  }
}

export function useInteractionHandler(sessionId: string | undefined) {
  const addInteraction = useInteractionStore((s) => s.addInteraction)
  const markResponded = useInteractionStore((s) => s.markResponded)
  const markNavigated = useInteractionStore((s) => s.markNavigated)
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

    const timers: ReturnType<typeof setTimeout>[] = []
    for (const item of completed) {
      if (!scheduledDismissals.current.has(item.requestId)) {
        scheduledDismissals.current.add(item.requestId)
        const timer = setTimeout(() => {
          scheduledDismissals.current.delete(item.requestId)
          dismissInteraction(item.requestId)
        }, 3000)
        timers.push(timer)
      }
    }

    return () => {
      timers.forEach(clearTimeout)
    }
  }, [pendingInteractions, dismissInteraction])

  useEffect(() => {
    const handleInteractionRequest = (data: Record<string, unknown>) => {
      const parsed = parseInteractionEvent(data)
      if (parsed) {
        addInteraction(parsed)
        playNotificationSound().catch(() => {})

        if (parsed.mode === 'conversation') {
          const agentTabStore = useAgentTabStore.getState()
          const pipelineId = parsed.pipelineId || parsed.agentId || parsed.requestId
          agentTabStore.openSubAgentTab({
            agentId: parsed.agentId || parsed.requestId,
            agentName: parsed.title || '人类交互',
            parentRecordId: parsed.requestId,
            agentLevel: 2,
            taskId: undefined,
            status: 'waiting_input',
            setActive: true,
            pipelineId,
          })
        }
      }
    }

    const handleInteractionCancelled = (data: Record<string, unknown>) => {
      const requestId = (data.request_id as string) || (data.requestId as string)
      if (requestId) {
        dismissInteraction(requestId)
      }
    }

    const handleInteractionTimeout = (data: Record<string, unknown>) => {
      const requestId = (data.request_id as string) || (data.requestId as string)
      if (requestId) {
        dismissInteraction(requestId)
      }
    }

    wsPool.subscribe(
      WS_SERVER_EVENTS.INTERACTION_REQUEST,
      handleInteractionRequest as any,
    )
    wsPool.subscribe(
      'interaction_cancelled',
      handleInteractionCancelled as any,
    )
    wsPool.subscribe(
      'interaction_timeout',
      handleInteractionTimeout as any,
    )

    return () => {
      wsPool.unsubscribe(
        WS_SERVER_EVENTS.INTERACTION_REQUEST,
        handleInteractionRequest as any,
      )
      wsPool.unsubscribe(
        'interaction_cancelled',
        handleInteractionCancelled as any,
      )
      wsPool.unsubscribe(
        'interaction_timeout',
        handleInteractionTimeout as any,
      )
    }
  }, [addInteraction, dismissInteraction])

  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      await wsPool.sendInteractionResponse(sid, {
        requestId,
        responseType: 'answered',
        selectedOption,
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  const respondConversation = useCallback(
    async (requestId: string, feedback: string) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      await wsPool.sendInteractionResponse(sid, {
        requestId,
        responseType: 'answered',
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  const navigateToTab = useCallback(
    async (requestId: string, threadId: string) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) return
      await wsPool.sendInteractionResponse(sid, {
        requestId,
        responseType: 'approved',
        feedback: '用户已进入对话',
      })
      markNavigated(requestId)

      if (!threadId) return

      const tabStore = useAgentTabStore.getState()
      tabStore.openSubAgentTab({
        agentId: threadId,
        agentName: '子任务',
        parentRecordId: threadId,
        agentLevel: 2,
        status: 'running',
        setActive: true,
      })

      try {
        await useSessionStore.getState().fetchMessages(threadId)
        const subMessages = useSessionStore.getState().messages[threadId] || []
        const tabId = `sub-${threadId}`
        for (const msg of subMessages) {
          tabStore.addMessageToTab(tabId, msg)
        }
      } catch (error) {
        console.error('[navigateToTab] 加载子任务消息失败:', error)
      }
    },
    [markNavigated],
  )

  return {
    pendingInteractions: sessionPending,
    respondChoice,
    respondConversation,
    navigateToTab,
  }
}
