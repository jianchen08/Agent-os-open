/**
 * useInteractionHandler Hook
 *
 * 业务编排层：订阅 WebSocket 交互事件 → 解析数据写入 store → 提供 actions 给 UI。
 * 单一职责：只处理人类交互相关逻辑。
 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { webSocketService } from '@/services/websocket/WebSocketService'
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
  // Backend sends { type, data: { request_id, ... } }
  // After WS destructuring { type, ...rest }, the inner data is at rest.data
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
    /** pipeline_id，优先从事件数据中获取，回退到 agentId 作为关联键 */
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

  // 追踪已调度清理的 requestId，防止重复调度
  const scheduledDismissals = useRef<Set<string>>(new Set())

  // 当前会话的活跃交互（pending + recently completed，用于展示完成状态）
  // 不过滤 threadId === sessionId：后端的 human_interaction 工具使用 pipeline_id 作为
  // threadId，与前端 WebSocket 注册的 session_id 不同，严格匹配会导致交互卡片无法显示。
  // WebSocket 连接本身就是按会话隔离的，所以收到的交互事件都属于当前会话。
  const sessionPending = useMemo(
    () =>
      sessionId
        ? pendingInteractions.filter((i) => i.status !== 'dismissed')
        : [],
    [pendingInteractions, sessionId],
  )

  // 已完成交互自动清理（3 秒后从列表移除）
  // 使用 ref 追踪已调度的清理任务，避免重复 setTimeout
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

  // 订阅 WebSocket 交互事件
  useEffect(() => {
    const handleInteractionRequest = (data: Record<string, unknown>) => {
      const parsed = parseInteractionEvent(data)
      if (parsed) {
        addInteraction(parsed)
        // 播放交互提示音（异步，不阻塞主线程）
        playNotificationSound().catch(() => {
          // 静默处理播放失败
        })

        // 对话模式：自动打开对应的对话子标签，同时注册 pipeline_id 映射
        if (parsed.mode === 'conversation') {
          const agentTabStore = useAgentTabStore.getState()
          // 优先使用 pipeline_id，如果没有则用 agentId 作为关联键
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

    webSocketService.subscribe(
      WS_SERVER_EVENTS.INTERACTION_REQUEST,
      handleInteractionRequest as any,
    )
    webSocketService.subscribe(
      'interaction_cancelled',
      handleInteractionCancelled as any,
    )
    webSocketService.subscribe(
      'interaction_timeout',
      handleInteractionTimeout as any,
    )

    return () => {
      webSocketService.unsubscribe(
        WS_SERVER_EVENTS.INTERACTION_REQUEST,
        handleInteractionRequest as any,
      )
      webSocketService.unsubscribe(
        'interaction_cancelled',
        handleInteractionCancelled as any,
      )
      webSocketService.unsubscribe(
        'interaction_timeout',
        handleInteractionTimeout as any,
      )
    }
  }, [addInteraction, dismissInteraction])

  // ---- Actions（给 UI 层调用） ----

  /** 选择模式：用户选中一个选项 */
  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      await webSocketService.sendInteractionResponse({
        requestId,
        responseType: 'answered',
        selectedOption,
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  /** 对话模式：用户发送文本回复 */
  const respondConversation = useCallback(
    async (requestId: string, feedback: string) => {
      await webSocketService.sendInteractionResponse({
        requestId,
        responseType: 'answered',
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  /** 对话模式：跳转到子标签对话页（Tab 内切换，不重新连接 WebSocket） */
  const navigateToTab = useCallback(
    async (requestId: string, threadId: string) => {
      await webSocketService.sendInteractionResponse({
        requestId,
        responseType: 'approved',
        feedback: 'user_navigated_to_tab',
      })
      markNavigated(requestId)

      if (!threadId) return

      // 通过 agentTabStore 打开/切换到子 Agent Tab（Tab 内切换，不改变 activeSessionId）
      const tabStore = useAgentTabStore.getState()
      tabStore.openSubAgentTab({
        agentId: threadId,
        agentName: '子任务',
        parentRecordId: threadId,
        agentLevel: 2,
        status: 'running',
        setActive: true,
      })

      // 加载子任务的历史消息到 Tab 消息列表
      try {
        await useSessionStore.getState().fetchMessages(threadId)
        // 将加载到的消息写入 tabMessages
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
    /** 当前会话的待处理交互 */
    pendingInteractions: sessionPending,
    /** 选择模式响应 */
    respondChoice,
    /** 对话模式文本响应 */
    respondConversation,
    /** 跳转到子标签对话页 */
    navigateToTab,
  }
}
