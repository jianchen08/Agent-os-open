/** useInteractionHandler Hook 业务编排层：订阅 WebSocket 交互事件 → 解析数据写入 store → 提供 actions 给 UI。 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { useNavigate } from 'react-router-dom'
import { API_ENDPOINTS } from '@/constants/api'
import { ROUTES } from '@/constants/routes'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import apiClient from '@/services/api/client'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useUIStore } from '@/stores/uiStore'
import { playNotificationSound } from '@/utils/audioNotification'
import type { PendingInteraction } from '@/stores/interactionStore'

/** 模块级标志位：防止多个组件调用 useInteractionHandler 时重复注册 WebSocket 事件订阅 */
let _isSubscribed = false

/**
 * 把后端 `/interaction/pending` 返回的 record（嵌套结构）适配为 `parseInteractionEvent` 期望的扁平结构。
 *
 * record 形如 `{ id, session_id, message_data: { request_id?, interaction_mode, ... } }`，
 * 而 WS payload 与 parseInteractionEvent 期望的是扁平 `{ request_id, interaction_mode, session_id, ... }`。
 * 这里把 message_data 拍平、用顶层 id 兜底 request_id，使恢复路径能复用同一套解析逻辑。
 */
function normalizeRecord(record: Record<string, unknown>): Record<string, unknown> {
  const messageData = (record.message_data as Record<string, unknown>) || {}
  return {
    ...messageData,
    request_id: messageData.request_id || (record.id as string) || '',
    session_id: (record.session_id as string) || '',
  }
}

/** 从 WebSocket interaction_request 事件数据解析为 PendingInteraction 后端传递 file_paths（文件路径列表），前端通过 file-content API 拉取实际内容 */

/**
 * options 归一化：后端/LLM 可能把选项发成字符串数组（["批准","拒绝"]），
 * 而 InteractionCard 渲染的是 opt.label（InteractionOption 对象数组）。
 * 字符串元素会导致按钮文字为空——表现为"审批卡片只有输入框、没有选项按钮"。
 * 另一类实测形态是对象包裹（{"item":["批准","拒绝"]}，MiniMax 传参畸形）：
 * 取其第一个数组值继续归一化。
 */
function normalizeOptions(raw: unknown): PendingInteraction['options'] {
  let list: unknown = raw
  if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
    const firstArray = Object.values(raw as Record<string, unknown>).find(
      (v) => Array.isArray(v),
    )
    list = firstArray
  }
  if (!Array.isArray(list)) return undefined
  const out = list
    .map((item, i) => {
      if (typeof item === 'string') return { id: String(i), label: item }
      if (item && typeof item === 'object') {
        const o = item as Record<string, unknown>
        const label = (o.label as string) || (o.text as string) || (o.name as string)
        if (!label) return null
        return {
          id: o.id != null ? String(o.id) : String(i),
          label,
          ...(o.description ? { description: o.description as string } : {}),
        }
      }
      return null
    })
    .filter((x): x is { id: string; label: string; description?: string } => x !== null)
  return out.length > 0 ? out : undefined
}

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
            WORKSPACE_SERVICE_ENDPOINTS.workspaces_file_content_get.replace('{container_task_id}', '_local'),
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
    options: normalizeOptions(inner.options),
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
    if (_isSubscribed) return
    _isSubscribed = true

    const requestToNotificationMap = new Map<string, string>()

    /**
     * 把已解析的交互写入对应 store（去重 + mode 分流）。
     * WS 实时推送与刷新恢复共用此入口，避免两套写入逻辑产生行为分叉。
     * 返回 true 表示这是一条新交互（之前不存在）。
     */
    const ingestParsedInteraction = (
      parsed: Omit<PendingInteraction, 'status'>,
    ): boolean => {
      const existing = useInteractionStore.getState().pendingInteractions.find(
        (i) => i.requestId === parsed.requestId,
      )
      if (existing) return false

      // choice/conversation 模式在通知中心产生冗余通知。
      // - choice/conversation 模式：只写入 interactionStore（交互卡片已在聊天区域展示）
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
      return true
    }

    const handleInteractionRequest = async (data: Record<string, unknown>) => {
      const parsed = await parseInteractionEvent(data)
      if (!parsed) {
        console.warn('[InteractionHandler] parseInteractionEvent returned null')
        return
      }

      const isNew = ingestParsedInteraction(parsed)
      if (!isNew) return

      // 避免重复通知。
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

      // tabId 形如 `file-${containerId}-${path}`，重复推送同一文件自动去重激活。
      if (parsed.fileContents && Object.keys(parsed.fileContents).length > 0) {
        const layoutStore = useLayoutModeStore.getState()
        const filePaths = Object.keys(parsed.fileContents)
        const containerId = '_local'
        let firstTabId: string | null = null

        for (const filePath of filePaths) {
          const fileName = filePath.split(/[/\\]/).pop() || filePath
          const tabId = `file-${containerId}-${filePath.replace(/[/\\]/g, '_')}`
          if (!firstTabId) firstTabId = tabId

          const existing = layoutStore.workspaceTabs.find((t) => t.id === tabId)
          if (existing) {
            continue
          }

          registerFileEditor(tabId, {
            filePath,
            fileName,
            content: parsed.fileContents[filePath] ?? '',
            containerTaskId: containerId,
          })
          layoutStore.addWorkspaceTab({
            id: tabId,
            title: fileName,
            icon: '📄',
            moduleId: '__file_editor__',
            isActive: false,
            isPinned: false,
          })
        }

        if (firstTabId) {
          layoutStore.setActiveTab(firstTabId)
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

    /** 1 秒防抖时间戳，避免重连风暴下重复拉取 pending 列表。 */
    let lastRestoreAt = 0

    /**
     * 从后端 `/interaction/pending` 拉取仍待处理的交互请求并恢复到 store。
     * 覆盖「页面刷新 / WS 重连」后交互卡片丢失的场景：WS 推送是 fire-and-forget，
     * 不重推历史请求，故由本函数在初始化与重连时主动拉取重建。
     * 复用 ingestParsedInteraction 完成去重与 mode 分流，addInteraction 自身也会去重。
     */
    const restorePendingInteractions = async () => {
      const now = Date.now()
      if (now - lastRestoreAt < 1000) return
      lastRestoreAt = now

      try {
        const resp = await apiClient.get(API_ENDPOINTS.INTERACTION.PENDING)
        const items = (resp.data?.items as Record<string, unknown>[]) || []
        for (const record of items) {
          const normalized = normalizeRecord(record)
          const parsed = await parseInteractionEvent(normalized)
          if (!parsed) continue
          ingestParsedInteraction(parsed)
        }
      } catch (err) {
        console.warn('[InteractionHandler] 恢复待处理交互失败:', err)
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
    // WS 重连后恢复 pending 交互（断线期间可能错过推送，或刷新后内存已清空）
    globalWS.subscribe('reconnected', restorePendingInteractions)

    // 挂载即拉取一次：覆盖纯刷新、WS 尚未触发 reconnected 的窗口
    restorePendingInteractions()

    return () => {
      globalWS.unsubscribe('_status', handleWsStatusChange as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.INTERACTION_REQUEST, handleInteractionRequest as any)
      globalWS.unsubscribe('interaction_cancelled', handleInteractionCancelled as any)
      globalWS.unsubscribe('interaction_timeout', handleInteractionTimeout as any)
      globalWS.unsubscribe('reconnected', restorePendingInteractions)
      _isSubscribed = false
    }
  }, [addInteraction, dismissInteraction])

  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      // 路由键只取交互自身坐标（2026-08-22 裁决）：sessionId → threadId，
      // 换不到即中止发信（fail-closed，交互保持 pending 可见可重试）——
      // 兜底 activeSessionId 会在用户切换会话后把审批发到错误 thread 通道。
      const interaction = useInteractionStore
        .getState()
        .pendingInteractions.find((i) => i.requestId === requestId)
      const sid = interaction?.sessionId || interaction?.threadId
      if (!sid) {
        console.warn('[InteractionHandler] respondChoice 中止: 交互无自身 sessionId/threadId!', requestId)
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
      // 同 respondChoice：路由键只取交互自身坐标（sessionId → threadId）
      const interaction = useInteractionStore
        .getState()
        .pendingInteractions.find((i) => i.requestId === requestId)
      const sid = interaction?.sessionId || interaction?.threadId
      if (!sid) {
        console.warn('[InteractionHandler] respondConversation 中止: 交互无自身 sessionId/threadId!', requestId)
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
    async (requestId: string, pipelineId: string, title?: string, agentLevelStr?: string) => {
      // 同 respondChoice/respondConversation：路由键只取交互自身坐标
      // （sessionId → threadId），不兜底全局活跃会话（2026-08-22 裁决）
      const interaction = useInteractionStore
        .getState()
        .pendingInteractions.find((i) => i.requestId === requestId)
      const currentSid = interaction?.sessionId || interaction?.threadId
      if (!currentSid) {
        console.error('[useInteractionHandler.navigateToTab] 交互无自身 sessionId/threadId，无法处理交互跳转', requestId)
        return
      }

      await globalWS.sendInteractionResponse(currentSid, requestId, {
        response_type: 'approved',
        feedback: '用户已进入对话标签页',
      })

      markEntered(requestId)

      if (!pipelineId) {
        console.error('[useInteractionHandler.navigateToTab] 交互请求缺少 pipelineId，无法跳转', requestId)
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
      await navigateToPipeline(pipelineId, {
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
