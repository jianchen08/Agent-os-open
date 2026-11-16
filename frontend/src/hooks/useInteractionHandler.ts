/** useInteractionHandler Hook 业务编排层：订阅 WebSocket 交互事件 → 解析数据写入 store → 提供 actions 给 UI。 */

import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { API_ENDPOINTS } from '@/constants/api'
import { ROUTES } from '@/constants/routes'
import { WS_LOCAL_EVENTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { registerFileEditor } from '@/stores/fileEditorRegistry'
import { useInteractionStore } from '@/stores/interactionStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useUIStore } from '@/stores/uiStore'
import { PRIORITY_STYLES } from '@/types/notification'
import { playNotificationSound } from '@/utils/audioNotification'
import { resolveInteractionSourceLabel } from '@/utils/interactionOrigin'
import { loggers } from '@/utils/logger'
import { showSystemNotification } from '@/utils/systemNotification'
import type { PendingInteraction } from '@/stores/interactionStore'
import type { NotificationPriority } from '@/types/notification'

/** 模块级标志位：防止多个组件调用 useInteractionHandler 时重复注册 WebSocket 事件订阅 */
let _isSubscribed = false

/** priority 白名单（P-144）：= PRIORITY_STYLES 键集（critical/high/normal/low 四档） */
const VALID_NOTIFICATION_PRIORITIES: ReadonlySet<string> = new Set(
  Object.keys(PRIORITY_STYLES),
)

/** 已告警过的非法 priority 值（同值只 warn 一次，轮询重放不刷屏；越界整表清防无界增长） */
const warnedInvalidPriorities = new Set<string>()

/**
 * priority 白名单校验（P-144）：human 插件/LLM 可能把 priority 发成白名单外
 * 的任意值（'urgent'/'Critical'/数字），直传 store 会在渲染层落
 * PRIORITY_STYLES[priority]=undefined（样式兜底缺失即崩）。白名单内原样透传
 * （含 critical 四档）；白名单外回退本入口默认档 high 并按值去重 warn 一次
 * （缺失/null/空串沿用既有静默默认，不算异常输入）。
 */
function normalizeNotificationPriority(raw: unknown): NotificationPriority {
  if (typeof raw === 'string' && VALID_NOTIFICATION_PRIORITIES.has(raw)) {
    return raw as NotificationPriority
  }
  const isAbsent = raw === undefined || raw === null || raw === ''
  if (!isAbsent && !warnedInvalidPriorities.has(String(raw))) {
    if (warnedInvalidPriorities.size >= 50) warnedInvalidPriorities.clear()
    warnedInvalidPriorities.add(String(raw))
    loggers.notification.warn('[P-144] notification priority 非法值，回退默认档 high: %j', raw)
  }
  return 'high'
}

/**
 * get_pending 兜底轮询间隔（BUG-36）：WS 推送是 fire-and-forget，任何一跳丢失
 * （瞬断、连接被替换未重连、内核投递失败）都会让审批卡在整个等待窗口内静默缺席，
 * 而恢复链只覆盖挂载与重连两个时刻。以有界间隔轮询 /interaction/pending 补偿，
 * ingest 去重保证幂等；间隔既不形成请求风暴，也不让用户干等一个超时窗口。
 */
export const INTERACTION_PENDING_POLL_INTERVAL_MS = 10_000

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
    created_at: (record.created_at as string) || '',
  }
}

/** 从 WebSocket interaction_request 事件数据解析为 PendingInteraction 后端传递 file_paths（文件路径列表），前端通过 file-content API 拉取实际内容 */

/**
 * options 归一化：后端/LLM 可能把选项发成字符串数组（["批准","拒绝"]），
 * 而 InteractionCard 渲染的是 opt.label（InteractionOption 对象数组）。
 * 字符串元素会导致按钮文字为空——表现为"审批卡片只有输入框、没有选项按钮"。
 * 另一类形态是对象包裹（{"item":["批准","拒绝"]}，MiniMax 传参畸形）：
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

/** parse 产物：交互对象（入 store）与文件全文（fileContents 仅在本次派发链路内消费，不入库）。 */
interface ParsedInteractionPayload {
  interaction: Omit<PendingInteraction, 'status'>
  fileContents?: Record<string, string>
}

async function parseInteractionEvent(
  data: Record<string, unknown>,
): Promise<ParsedInteractionPayload | null> {
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

  const interaction: Omit<PendingInteraction, 'status'> = {
    requestId,
    mode: (inner.interaction_mode as 'choice' | 'conversation' | 'notification') || 'choice',
    title: (inner.title as string) || '',
    description: (inner.description as string) || '',
    threadId,
    tabId: (inner.tab_id as string) || '',
    agentId: (inner.agent_id as string) || '',
    pipelineId,
    options: normalizeOptions(inner.options),
    initialMessage: inner.initial_message as string,
    suggestions: inner.suggestions as string[],
    priority: inner.priority as PendingInteraction['priority'],
    progress: inner.progress as number | undefined,
    // 审批卡倒计时（BUG-14）：后端已收敛的等待上限 + 创建时刻
    timeoutSeconds: (inner.timeout_seconds as number) || undefined,
    createdAt: (inner.created_at as string) || undefined,
    timestamp: new Date().toISOString(),
    agentLevel: rawAgentLevel || undefined,
    agentName: (inner.agent_name as string) || undefined,
    sessionId,
  }
  return fileContents ? { interaction, fileContents } : { interaction }
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
        // notification 模式：只写入通知中心，不写入交互 Store。去重不能靠
        // interactionStore（这里从不写入）——用后端 request_id 作通知稳定 id，
        // 由通知中心的幂等闸兜住 /interaction/pending 兜底轮询对同一条
        // forever-pending 通知的反复重放（BUG-74：同 id 重复入列、badge 无限增长）。
        const wasListed = useNotificationStore
          .getState()
          .notifications.some((n) => n.id === parsed.requestId)
        useNotificationStore.getState().addNotification({
          id: parsed.requestId,
          title: parsed.title || '人类交互请求',
          message: parsed.description || `${parsed.agentId || 'Agent'} 请求您的输入`,
          priority: normalizeNotificationPriority(parsed.priority),
          category: 'alert',
          isBlocking: false,
          sourceLabel: resolveInteractionSourceLabel(parsed),
        })
        // 入列复核：被幂等闸挡回（已在列 → 仅更新；已被用户移除 → 不回弹）
        // 时不算新交互，提示音/系统级通知不得随轮询重放反复触发。
        const isListed = useNotificationStore
          .getState()
          .notifications.some((n) => n.id === parsed.requestId)
        return isListed && !wasListed
      }
      // choice/conversation 模式：只写入交互 Store，由 GlobalInteractionOverlay 全局展示
      addInteraction(parsed)
      return true
    }

    const handleInteractionRequest = async (data: Record<string, unknown>) => {
      const parsed = await parseInteractionEvent(data)
      if (!parsed) {
        console.warn('[InteractionHandler] parseInteractionEvent returned null')
        return
      }

      const { interaction, fileContents } = parsed
      const isNew = ingestParsedInteraction(interaction)
      if (!isNew) return

      // 系统级通知：Electron 下经宿主 OS 原生通知呈现（Windows toast /
      // macOS 通知中心 / Linux libnotify）；Web 环境无桥接自动跳过。
      // 携带交互自身 threadId：点击通知跳转对应对话（无 threadId 退化为仅聚焦）。
      // 不 await：通知失败不阻断交互卡片与提示音链路。
      void showSystemNotification({
        title: interaction.title || '人类交互请求',
        body: interaction.description || `${interaction.agentId || 'Agent'} 请求您的输入`,
        threadId: interaction.threadId || undefined,
      })

      // 避免重复通知。
      playNotificationSound().catch(() => {
        if (interaction.mode === 'notification') return
        useNotificationStore.getState().addNotification({
          title: interaction.title || '人类交互请求',
          message: interaction.description || `${interaction.agentId || 'Agent'} 请求您的输入（音频通知失败）`,
          priority: normalizeNotificationPriority(interaction.priority),
          category: 'alert',
          isBlocking: false,
          autoDismissMs: 8000,
          sourceLabel: resolveInteractionSourceLabel(interaction),
        })
      })

      // tabId 形如 `file-${containerId}-${path}`，重复推送同一文件自动去重激活。
      if (fileContents && Object.keys(fileContents).length > 0) {
        const layoutStore = useLayoutModeStore.getState()
        const filePaths = Object.keys(fileContents)
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
            content: fileContents[filePath] ?? '',
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

    const handleWsStatusChange = (data: Record<string, unknown>) => {
      if (data.status === 'disconnected') {
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

      // 轮询发起时刻：此前入列的本地 pending 才参与对账（之后 WS 事件入列的
      // 卡片豁免本轮——响应可能先于入列生成，防止误收在途卡）
      const pollStartedAt = new Date().toISOString()

      try {
        const resp = await apiClient.get(API_ENDPOINTS.INTERACTION.PENDING)
        const items = (resp.data?.items as Record<string, unknown>[]) || []
        const serverIds = new Set<string>()
        for (const record of items) {
          const normalized = normalizeRecord(record)
          const parsed = await parseInteractionEvent(normalized)
          if (!parsed) {
            // 静默丢弃 = 审批无人能批（BUG-36 教训）：形状异常必须留痕可排查
            console.warn('[InteractionHandler] pending 记录解析失败，跳过恢复:', record)
            continue
          }
          serverIds.add(parsed.interaction.requestId)
          ingestParsedInteraction(parsed.interaction)
        }
        // 对账收卡（真机缺陷"审批交互卡残影浮层不自动收"）：本地仍 pending、
        // 服务端列表已无、且入列早于本轮轮询 = 已在他端结算（应答/取消/超时），
        // 自动收起浮层卡；浮层随 pending 清空卸载，手动关闭按钮不受影响。
        const { pendingInteractions } = useInteractionStore.getState()
        for (const item of pendingInteractions) {
          if (item.status !== 'pending') continue
          if (serverIds.has(item.requestId)) continue
          if (item.timestamp >= pollStartedAt) continue
          dismissInteraction(item.requestId)
        }
      } catch (err) {
        // 记日志即止：恢复失败时逐条循环已留痕，此处为整链失败兜底；
        // WS 实时事件仍会继续送达交互（OBS-R258-1 吞错误规则登记）
        console.warn('[InteractionHandler] 恢复待处理交互失败:', err)
      }
    }

    globalWS.subscribe(WS_LOCAL_EVENTS.STATUS, handleWsStatusChange)

    /**
     * 审批已在他端结算（approval.taken；多前端并发应答对账，ADR 2026-10-01
     * 决策 5）：按 request_id 清除本地 pending 交互并提示"已在别处处理"。
     * 本页没有该 pending 卡时静默（对账提示只面向持卡端，不无故打扰）。
     */
    const handleApprovalTaken = (data: Record<string, unknown>) => {
      const inner = (data.data as Record<string, unknown>) || data
      const requestId = typeof inner.request_id === 'string' ? inner.request_id : ''
      if (!requestId) return
      const existed = useInteractionStore
        .getState()
        .pendingInteractions.some((i) => i.requestId === requestId)
      if (!existed) return
      dismissInteraction(requestId)
      useNotificationStore.getState().addNotification({
        title: '审批已在别处处理',
        message: '该审批已在其他前端页面处理，本页卡片已同步关闭。',
        priority: 'normal',
        category: 'info',
        isBlocking: false,
        autoDismissMs: 8000,
        sourceLabel: '审批',
      })
    }

    globalWS.subscribe(
      WS_SERVER_EVENTS.INTERACTION_REQUEST,
      handleInteractionRequest,
    )
    globalWS.subscribe(WS_SERVER_EVENTS.APPROVAL_TAKEN, handleApprovalTaken)

    /**
     * 超时前提醒（P-20/U-2 订阅腿；后端 35b920af9 修复 payload.thread_id
     * 路由后可达）：后端每请求提醒一次（timeout - remind_before 时刻）。
     * 持卡端置提醒态（InteractionCard 倒计时切高亮「即将超时」）+ 提示
     * 一次（稳定 id 防重放重复）；非持卡端静默（APPROVAL_TAKEN 同纪律：
     * 提醒只面向正在等待的端，不无故打扰）。
     */
    const handleTimeoutReminder = (data: Record<string, unknown>) => {
      const inner = (data.data as Record<string, unknown>) || data
      const requestId = typeof inner.request_id === 'string' ? inner.request_id : ''
      if (!requestId) return
      const held = useInteractionStore
        .getState()
        .pendingInteractions.some((i) => i.requestId === requestId && i.status === 'pending')
      if (!held) return
      useInteractionStore.getState().markTimeoutReminded(requestId)
      const remainingSeconds =
        typeof inner.remaining_seconds === 'number' ? inner.remaining_seconds : null
      useNotificationStore.getState().addNotification({
        id: `interaction-timeout-reminder-${requestId}`,
        title: '审批即将超时',
        message:
          remainingSeconds != null
            ? `剩余 ${remainingSeconds} 秒，超时将自动按拒绝裁决，请尽快处理。`
            : '超时将自动按拒绝裁决，请尽快处理。',
        priority: 'high',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 0,
        sourceLabel: '审批',
      })
    }
    globalWS.subscribe(
      WS_SERVER_EVENTS.INTERACTION_TIMEOUT_REMINDER,
      handleTimeoutReminder,
    )
    // WS 重连后恢复 pending 交互（断线期间可能错过推送，或刷新后内存已清空）
    globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, restorePendingInteractions)

    // 挂载即拉取一次：覆盖纯刷新、WS 尚未触发 reconnected 的窗口
    restorePendingInteractions()

    // 兜底轮询（BUG-36）：推送丢失时按有界间隔补偿拉取，审批卡不再依赖
    // 「推送恰好送达」这一单点。restorePendingInteractions 自带去重与防抖。
    const pendingPollTimer = setInterval(
      () => void restorePendingInteractions(),
      INTERACTION_PENDING_POLL_INTERVAL_MS,
    )

    return () => {
      clearInterval(pendingPollTimer)
      globalWS.unsubscribe(WS_LOCAL_EVENTS.STATUS, handleWsStatusChange)
      globalWS.unsubscribe(WS_SERVER_EVENTS.INTERACTION_REQUEST, handleInteractionRequest)
      globalWS.unsubscribe(WS_SERVER_EVENTS.APPROVAL_TAKEN, handleApprovalTaken)
      globalWS.unsubscribe(
        WS_SERVER_EVENTS.INTERACTION_TIMEOUT_REMINDER,
        handleTimeoutReminder,
      )
      globalWS.unsubscribe(WS_LOCAL_EVENTS.RECONNECTED, restorePendingInteractions)
      _isSubscribed = false
    }
  }, [addInteraction, dismissInteraction])

  const respondChoice = useCallback(
    async (requestId: string, selectedOption?: string, feedback?: string) => {
      // 路由键只取交互自身坐标：sessionId → threadId，
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
      // （sessionId → threadId），不兜底全局活跃会话
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

      // 使用全局管道导航服务跳转（自动处理跨会话查找和标签创建）。
      // 标签命名链（2026-10-16 用户拍板，全标签统一）：交互标题 → 智能体名 → 泛称
      await navigateToPipeline(pipelineId, {
        agentName: title || interaction?.agentName || '对话',
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
