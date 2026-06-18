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
import apiClient from '@/services/api/client'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { registerFileReview, getFileReviewData } from '@/stores/fileReviewRegistry'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { playNotificationSound } from '@/utils/audioNotification'
import type { PendingInteraction } from '@/stores/interactionStore'

/** 模块级标志位：防止多个组件调用 useInteractionHandler 时重复注册 WebSocket 事件订阅 */
let _isSubscribed = false

/**
 * 从 WebSocket interaction_request 事件数据解析为 PendingInteraction
 * 后端传递 file_paths（文件路径列表），前端通过 file-content API 拉取实际内容
 * API 已改造为 fallback 到 cwd，不再依赖 container_task_id
 */
async function parseInteractionEvent(
  data: Record<string, unknown>,
): Promise<Omit<PendingInteraction, 'status'> | null> {
  const inner = (data.data as Record<string, unknown>) || data

  const requestId = inner.request_id as string
  if (!requestId) return null

  const rawAgentLevel = (inner.agent_level as string || '').toUpperCase()

  const filePaths = inner.file_paths as string[] | undefined
  let fileContents: Record<string, string> | undefined
  if (filePaths && filePaths.length > 0) {
    const contents: Record<string, string> = {}
    const failedPaths: string[] = []
    await Promise.all(
      filePaths.map(async (filePath) => {
        try {
          const resp = await apiClient.get(
            `/api/v1/workspaces/_local/file-content`,
            { params: { path: filePath } },
          )
          if (resp.data?.success) {
            contents[filePath] = resp.data.content ?? ''
          } else {
            failedPaths.push(filePath)
            contents[filePath] = `⚠️ 文件加载失败: ${resp.data?.message || '未知错误'}`
          }
        } catch (err) {
          console.warn('[InteractionHandler] API failed for', filePath, ':', err)
          failedPaths.push(filePath)
          contents[filePath] = `⚠️ 文件加载失败: ${err instanceof Error ? err.message : '网络错误'}`
        }
      }),
    )
    if (failedPaths.length > 0) {
      console.warn('[InteractionHandler] 部分文件加载失败:', failedPaths)
    }
    if (Object.keys(contents).length > 0) {
      fileContents = contents
    }
  }

  const sessionId = inner.session_id as string | undefined

  // 路由关键字段为空会导致后续路由失败，显式告警
  const threadId = (inner.thread_id as string) || ''
  const pipelineId = (inner.pipeline_id as string) || ''
  if (!threadId) {
    console.warn('[useInteractionHandler] thread_id 缺失，交互路由可能失败', inner)
  }
  if (!pipelineId) {
    console.warn('[useInteractionHandler] pipeline_id 缺失，管道路由可能失败', inner)
  }

  return {
    requestId,
    mode: (inner.interaction_mode as 'choice' | 'conversation' | 'notification') || 'choice',
    title: (inner.title as string) || '',
    description: (inner.description as string) || '',
    threadId,
    tabId: (inner.tab_id as string) || '',
    agentId: (inner.agent_id as string) || '',
    pipelineId,
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

  // BUG-FIX-fix_20260531_interaction_duplicate:
  // 问题根因: useInteractionHandler 被 InteractionPanel 和 GlobalInteractionOverlay
  //   两个组件同时调用，导致 WebSocket 事件处理器注册两次，每个事件触发两次回调。
  // 修复方案: 使用模块级标志位 _isSubscribed 确保全局只注册一次。
  // 影响范围: WebSocket 交互事件订阅
  // 修复日期: 2026-05-31
  useEffect(() => {
    if (_isSubscribed) return
    _isSubscribed = true

    const requestToNotificationMap = new Map<string, string>()

    const handleInteractionRequest = async (data: Record<string, unknown>) => {
      const parsed = await parseInteractionEvent(data)
      if (!parsed) {
        console.warn('[InteractionHandler] parseInteractionEvent returned null')
        return
      }
      const existing = useInteractionStore.getState().pendingInteractions.find(
        (i) => i.requestId === parsed.requestId,
      )
      if (existing) return

      // BUG-FIX-fix_20260531_interaction_duplicate:
      // 问题根因: 所有交互模式同时写入 interactionStore 和 notificationStore，
      //   导致 notification 模式在聊天区域和通知中心重复显示，
      //   choice/conversation 模式在通知中心产生冗余通知。
      // 修复方案: 按交互模式分流 Store 写入：
      //   - notification 模式：只写入 notificationStore（纯通知，不需要用户交互）
      //   - choice/conversation 模式：只写入 interactionStore（交互卡片已在聊天区域展示）
      // 影响范围: 人类交互请求的展示逻辑
      // 修复日期: 2026-05-31
      if (parsed.mode === 'notification') {
        // notification 模式：只写入通知中心，不写入交互 Store
        const notifId = useNotificationStore.getState().addNotification({
          title: parsed.title || '人类交互请求',
          message: parsed.description || `${parsed.agentId || 'Agent'} 请求您的输入`,
          priority: (parsed.priority as 'high' | 'normal' | 'low') || 'high',
          category: 'alert',
          isBlocking: false,
        })
        requestToNotificationMap.set(parsed.requestId, notifId)
      } else {
        // choice/conversation 模式：只写入交互 Store，由 GlobalInteractionOverlay 全局展示
        addInteraction(parsed)
      }

      // BUG-FIX-fix_20260617_silent_audio_catch:
      // 问题根因: 原代码 playNotificationSound().catch(() => {}) 静默吞异常，
      //          AI 请求人类交互时音频通知失败用户无感知。
      // 修复方案: 音频失败时通过视觉通知兜底。notification 模式已通过上方
      //          addNotification 通知用户，此处仅对 choice/conversation 模式补充视觉兜底，
      //          避免重复通知。
      playNotificationSound().catch(() => {
        if (parsed.mode === 'notification') return
        useNotificationStore.getState().addNotification({
          title: parsed.title || '人类交互请求',
          message: parsed.description || `${parsed.agentId || 'Agent'} 请求您的输入（音频通知失败）`,
          priority: (parsed.priority as 'high' | 'normal' | 'low') || 'high',
          category: 'alert',
          isBlocking: false,
          autoDismissMs: 8000,
        })
      })

      if (parsed.fileContents && Object.keys(parsed.fileContents).length > 0) {
        const layoutStore = useLayoutModeStore.getState()

        // 检查是否已有相同文件的标签页打开，如果有则直接跳转
        const filePaths = Object.keys(parsed.fileContents)
        const existingTab = layoutStore.workspaceTabs.find((t) => {
          // 匹配文件编辑器标签（tabId 格式：file-local-${sanitizedPath}）
          if (t.moduleId === '__file_editor__') {
            return filePaths.some((fp) => {
              const editorTabId = `file-local-${fp.replace(/[/\\]/g, '_')}`
              return t.id === editorTabId
            })
          }
          // 匹配文件审阅标签（tabId 格式：review-${requestId}），检查注册表中文件路径是否相同
          if (t.moduleId === '__file_review__') {
            const reviewData = getFileReviewData(t.id)
            if (reviewData) {
              const reviewFiles = Object.keys(reviewData.fileContents)
              return filePaths.length === reviewFiles.length
                && filePaths.every((fp) => reviewFiles.includes(fp))
            }
          }
          return false
        })

        if (existingTab) {
          // 已有相同文件的标签，直接激活跳转
          layoutStore.setActiveTab(existingTab.id)
        } else {
          // 没有已打开的相同文件标签，创建新的审阅标签
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
          layoutStore.addWorkspaceTab({
            id: tabId,
            title: parsed.title || '文件审阅',
            icon: '📄',
            moduleId: '__file_review__',
            isActive: true,
            isPinned: false,
          })
          layoutStore.setActiveTab(tabId)
        }
        useLayoutModeStore.getState().setMode('five-space')
        useUIStore.getState().setWorkspaceCollapsed(false)
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
      const inner = (data.data as Record<string, unknown>) || data
      const requestId = inner.request_id as string
      if (requestId) {
        dismissInteraction(requestId)
        removeNotificationForRequest(requestId)
      }
    }

    const handleInteractionTimeout = (data: Record<string, unknown>) => {
      const inner = (data.data as Record<string, unknown>) || data
      const requestId = inner.request_id as string
      if (requestId) {
        dismissInteraction(requestId)
        removeNotificationForRequest(requestId)
      }
    }

    const handleWsStatusChange = (data: Record<string, unknown>) => {
      if ((data as any).status === 'disconnected') {
        _isSubscribed = false
      }
    }

    globalWS.subscribe('_status', handleWsStatusChange as any)

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
      globalWS.unsubscribe('_status', handleWsStatusChange as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.INTERACTION_REQUEST, handleInteractionRequest as any)
      globalWS.unsubscribe('interaction_cancelled', handleInteractionCancelled as any)
      globalWS.unsubscribe('interaction_timeout', handleInteractionTimeout as any)
      _isSubscribed = false
    }
  }, [addInteraction, dismissInteraction])

  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) {
        console.warn('[InteractionHandler] respondChoice 中止: activeSessionId 为空!')
        return
      }
      await globalWS.sendInteractionResponse(sid, requestId, {
        response_type: 'answered',
        selected_option: selectedOption,
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  const respondConversation = useCallback(
    async (requestId: string, feedback: string) => {
      const sid = useSessionStore.getState().activeSessionId
      if (!sid) {
        console.warn('[InteractionHandler] respondConversation 中止: activeSessionId 为空!')
        return
      }
      await globalWS.sendInteractionResponse(sid, requestId, {
        response_type: 'answered',
        feedback,
      })
      markResponded(requestId)
    },
    [markResponded],
  )

  const navigateToTab = useCallback(
    async (requestId: string, threadId: string, title?: string, agentLevelStr?: string, interactionSessionId?: string) => {
      const currentSid = useSessionStore.getState().activeSessionId
      if (!currentSid) {
        console.error('[useInteractionHandler.navigateToTab] 无活跃会话，无法处理交互跳转', requestId)
        return
      }

      await globalWS.sendInteractionResponse(currentSid, requestId, {
        response_type: 'approved',
        feedback: '用户已进入对话标签页',
      })

      markEntered(requestId)

      if (!threadId) {
        console.error('[useInteractionHandler.navigateToTab] 交互请求缺少 threadId，无法跳转', requestId)
        return
      }

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
