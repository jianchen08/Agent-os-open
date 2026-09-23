/** useRealtimeEvents Hook 订阅实时 WebSocket 事件并路由到 layout mode store 进行展示。 */

import { useEffect } from 'react'
import { WS_LOCAL_EVENTS, WS_SERVER_EVENTS } from '@/constants/websocket'
import {
  readLongTermTasks,
  updateLongTermTasksCache,
  invalidateLongTermTasks,
} from '@/hooks/queries/useLongTermTasksQuery'
import {
  invalidatePipelineRuns,
  invalidatePipelineStates,
} from '@/hooks/queries/usePipelineRunsQuery'
import { invalidateSessions, readSessions  } from '@/hooks/queries/useSessionsQuery'
import * as tokenLifecycle from '@/services/auth/tokenLifecycle'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { extractThreadId } from '@/services/websocket/streaming/handlers/utils'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { mainPipelineIdOf } from '@/utils/mappers'

/** Hook to subscribe to real-time WebSocket events and update the layout store. Call once in a top-level component (e.g. FiveSpaceHomePage). */
export function useRealtimeEvents(): void {
  const bumpWorkspaceDataVersion = useLayoutModeStore((s) => s.bumpWorkspaceDataVersion)

  useEffect(() => {
    // 防抖用时间戳记录，跟踪上次 fetchMessages 调用时间
    const lastFetchTimeRef = { current: 0 }

    /** WS 重连后重新加载当前会话消息，1 秒防抖避免频繁调用。 流式事件（stream_start 等）由 streaming/index.ts 统一处理，此处不重复订阅。 */
    const handleWsReconnect = () => {
      // 重连后补拉管道运行快照（断线期间的 stream_* 增量丢失，以快照对账）。
      // query 化：invalidate runs/states——活跃订阅自动重拉，替代原 store.fetch()。
      // 事件路径「先 invalidate 再取」强制新鲜：staleTime 窗口内直接 fetchQuery
      // 会拿旧缓存，必须失效后由订阅重拉。
      invalidatePipelineRuns()
      invalidatePipelineStates()
      // 断线期间可能有管道出生（任务提交/子任务派发）——会话列表无出生事件源，
      // 唯有在此失效重拉，否则 pipelineIds 陈旧导致任务管道跳转报"找不到"。
      invalidateSessions()
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId } = useSessionStore.getState()
      const sessions = readSessions()
      if (!activeSessionId) return
      // 只补当前会话的【主管道】（映射真值 pipelineIds[0]），
      // 不对 session.pipelineIds 全部扇出。
      // 子管道的消息在用户切到对应 tab 时按需加载。
      const session = sessions.find((s) => s.id === activeSessionId)
      const mainPipelineId = session ? mainPipelineIdOf(session) : undefined
      if (!mainPipelineId) return

      // 走 backfill（after_sequence 尾部游标读）而非 init（全量替换）：
      // 0.2 消息分页契约由内核持读面（游标已下推存储层）——backfill
      // 是 O(增量窗口) 的查询，重连补漏秒级；init 全量替换会丢弃刷新前
      // 的一切本地状态，重连场景不需要。
      usePipelineMessageStore
        .getState()
        .loadPipelineMessages(mainPipelineId, {
          threadId: activeSessionId,
          mode: 'backfill',
          skipStreamingCheck: true,
        })
        .then((result) => {
          if (!result.ok) {
            useNotificationStore.getState().addNotification({
              title: '消息同步失败',
              message: 'WebSocket 重连后消息同步失败，请手动刷新页面',
              priority: 'high',
              category: 'error',
              isBlocking: false,
              autoDismissMs: 8000,
              sourceLabel: '前端',
            })
          }
        })
    }

    // 2026-08 清理：execution_start/progress/output/done/cancelled、
    // sub_agent_created/waiting_input/completed、schema_updated 的订阅已删除——
    // 后端（kernel ws_session.rs / capability_router.rs 事件族 + 插件 event-bus.emit
    // 全集）无这些事件名的发射源，订阅是死代码。

    // Task lifecycle handlers

    // 订阅 task_status_update，触发工作区刷新并更新长期任务缓存中的任务状态
    // （query 化：已存在 → updateLongTermTasksCache 单任务增量，零请求；
    //   不存在 → invalidateLongTermTasks，活跃订阅自动重拉替代原 fetchTasks 全量）
    const handleTaskStatusUpdate = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const taskId = (data.task_id || data.taskId) as string | undefined
      const newStatus = data.new_status as string | undefined
      const currentPhase = data.current_phase as string | undefined

      if (taskId && newStatus) {
        const exists = readLongTermTasks().some((t) => t.id === taskId)
        if (exists) {
          const updates: Record<string, unknown> = { status: newStatus }
          if (currentPhase) {
            updates.currentPhase = currentPhase
          }
          const errorMsg = data.error as string | undefined
          if (errorMsg) {
            updates.error = errorMsg
          }
          updateLongTermTasksCache((prev) =>
            prev.map((t) => (t.id === taskId ? { ...t, ...updates } : t)),
          )
        } else {
          invalidateLongTermTasks()
        }
      }

      bumpWorkspaceDataVersion()
    }

    const handleTaskDeleted = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const taskId = (data.task_id || data.taskId) as string | undefined

      if (taskId) {
        // query 化：store.deleteTask 内部写 query cache（移除任务 + 清 activeTaskId）
        useLongTermTaskStore.getState().deleteTask(taskId)
      }

      bumpWorkspaceDataVersion()
    }

    /** 处理 TaskService 状态机变更事件（running/completed/failed 等切换） */
    const handleTaskStatusChanged = () => {
      bumpWorkspaceDataVersion()
    }

    // Subscribe to all events

    // WebSocket lifecycle（仅重连时补漏，首次连接由 setActiveSession 负责加载）
    globalWS.subscribe(WS_LOCAL_EVENTS.RECONNECTED, handleWsReconnect)

    // Task lifecycle events
    // （task_status_update / task_status_changed 当前后端推送路径静默跳过、
    //   待 SDK frontend.emit capability 落地后恢复——见 tasks/service.py，故保留订阅）
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted)

    /**
     * 发送失败透传（任何错误都必须让用户看见）：
     * user_input 断线排队超 TTL（连接迟迟未恢复）→ 撤"思考中"占位气泡、
     * 停止流式态、在原位置插入 system 错误消息 + 通知中心高优告警。
     */
    const handleUserInputSendTimeout = (eventData: {
      data?: {
        thread_id?: string
        pipeline_id?: string
        client_message_id?: string
        reason?: string
      }
    }) => {
      const info = eventData?.data || {}
      const pipelineId = info.pipeline_id || ''
      const cmid = info.client_message_id || ''
      const reason = info.reason || '连接断开，消息未送达'

      const ps = usePipelineMessageStore.getState()
      if (pipelineId) {
        if (cmid) {
          // [来源: docs/decisions/2026-08-22-streaming-protocol-rewrite.md]：
          // 乐观 user 在主数组（单一消息数组），发送失败标记 failed
          // （可重试，复用 cmid 幂等重发）——消息不消失、位置不丢
          ps.updateMessage(pipelineId, cmid, { status: 'failed' })
        }
        ps.stopStreaming(pipelineId)
      }

      // 原位置插入可见错误（system 消息有独立渲染分支），用户刷新后由后端权威内容对账
      if (pipelineId && cmid) {
        ps.addMessage(pipelineId, {
          id: `send_failed_${cmid}`,
          sessionId: info.thread_id || '',
          role: 'system',
          content: `⚠ ${reason}。这条消息没有发到服务器（后端无记录），请检查连接状态后重新发送。`,
          timestamp: new Date().toISOString(),
          status: 'error',
        } as never)
      }

      useNotificationStore.getState().addNotification({
        title: '消息发送失败',
        message: `${reason}，请检查连接状态后重新发送。`,
        priority: 'high',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 10000,
        sourceLabel: '前端',
      })
    }
    globalWS.subscribe(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, handleUserInputSendTimeout)

    /**
     * pending 输入队列同步（ADR-2026-08-26）：内核入队/消费/修改/删除时推送
     * 全量列表，前端据此刷新队列条（执行中发送的消息在等待窗口内可见可管理）。
     * action="returned"（用户裁定 2026-09-11）：停止生成时内核同步清队列并把
     * 退回条目随事件发还——回填输入框（追加语义，保留已有草稿）+ 清空队列条。
     */
    const handlePendingInputsChanged = (eventData: {
      data?: { pipeline_id?: string; action?: string; items?: unknown[] }
    }) => {
      const data = eventData?.data
      const pipelineId = data?.pipeline_id
      if (!pipelineId) return
      const items = (data?.items ?? []) as import('@/services/api/pipelines').PendingInputItem[]
      if (data?.action === 'returned') {
        // 只回填用户消息：trigger/task/system 来源的条目（如子任务完成通知）
        // 不是用户输入，退回输入框只会刷屏——丢弃（内核已一并出队）
        const contents = items
          .filter((item) => item.source === 'user')
          .map((item) => item.content)
          .filter((c): c is string => Boolean(c))
          .join('\n')
        if (contents) useChatInputStore.getState().requestInsert(contents)
        usePendingInputStore.getState().syncFromEvent(pipelineId, [])
        return
      }
      usePendingInputStore.getState().syncFromEvent(pipelineId, items)
    }
    globalWS.subscribe(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, handlePendingInputsChanged)

    /**
     * 上下文压缩彻底失败（context_window_guard 经 frontend.emit 透传）：
     * 压缩失败不阻塞管线（fail-open 降级），但上下文会持续膨胀——后端
     * 已按故障周期去重（连续失败只推一次，成功后复位），前端照常弹通知。
     */
    const handleCompressionFailed = (eventData: {
      data?: { thread_id?: string; pipeline_id?: string; message?: string }
    }) => {
      const data = eventData?.data || {}
      useNotificationStore.getState().addNotification({
        title: '上下文压缩失败',
        message: data.message || '会话继续但上下文将持续膨胀，建议关注会话状态或新建会话。',
        priority: 'high',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 8000,
        sourceLabel: '上下文压缩',
      })
    }
    globalWS.subscribe(WS_SERVER_EVENTS.COMPRESSION_FAILED, handleCompressionFailed)

    /**
     * 压缩波次完成（context_window_guard 压缩成功时 frontend.emit 透传）：
     * 压缩以 set(seq, 块/null) ops 原地重写 message_slots → 内容寻址指纹变异，
     * 前端跨压缩持有的 recordId 过期（回退/编辑重发出站 wire id miss）。
     * 收到后按 API 权威全量对账刷新（fetchMessages 无游标 → initFromAPI：
     * id/cmid/recordId 三键收敛，API 权威版自带新指纹；飞行中窗口保护在途
     * 消息不被误清）。去抖 500ms：事件在插件 execute 内发出、槽位 ops 由引擎
     * 在 execute 返回后落库，立即拉取的快照可能早于改写落库；同管道连续波次
     * 也合并为一次。流式中不跳过——压缩正发生在管道运行内，这是唯一对账时机
     * （loadPipelineMessages/reconcileFromAPI 的流式跳过语义在此不适用）。
     */
    const COMPRESSION_RECONCILE_DEBOUNCE_MS = 500
    const compressionReconcileTimers = new Map<string, ReturnType<typeof setTimeout>>()
    const handleCompressionApplied = (eventData: {
      data?: { thread_id?: string; pipeline_id?: string }
    }) => {
      const pipelineId = eventData?.data?.pipeline_id || ''
      const threadId = eventData?.data?.thread_id || ''
      if (!pipelineId || !threadId) {
        console.warn('[COMPRESSION_APPLIED] 坐标缺失，跳过对账', eventData?.data)
        return
      }
      const pending = compressionReconcileTimers.get(pipelineId)
      if (pending) clearTimeout(pending)
      compressionReconcileTimers.set(
        pipelineId,
        setTimeout(() => {
          compressionReconcileTimers.delete(pipelineId)
          usePipelineMessageStore
            .getState()
            .fetchMessages(pipelineId, { threadId })
            .catch(() => {
              // 对账失败 = 旧指纹残留，回退/编辑重发可能失败（内核账本回溯兜底）
              // ——必须让用户可见，静默会把故障留到操作失败那一刻才暴露。
              useNotificationStore.getState().addNotification({
                title: '压缩后消息对账失败',
                message: '上下文压缩后消息同步失败，回退/编辑重发可能失败，建议刷新页面重试。',
                priority: 'normal',
                category: 'error',
                isBlocking: false,
                autoDismissMs: 8000,
                sourceLabel: '上下文压缩',
              })
            })
        }, COMPRESSION_RECONCILE_DEBOUNCE_MS),
      )
    }
    globalWS.subscribe(WS_SERVER_EVENTS.COMPRESSION_APPLIED, handleCompressionApplied)

    /**
     * 段激活 ack（消息段模型 §5.1 WS 面：‹i/n› 多代切换 / 多端激活的对账事件）。
     * 服务端按后缀语义整段替换落库后推送，防抖 500ms 复用压缩对账
     * compressionReconcileTimers 模式：替换落库与事件发出存在竞态窗口，立即
     * 拉取的快照可能早于写库；多端连续激活合并为一次。对账 = 消息全量重拉
     * （initFromAPI 按 id/cmid/recordId 三键权威替换，乐观段视图被启用序列
     * 覆盖）+ 段清单刷新（激活会把切换前后缀冻结为新段，spanIndex 随之增长）
     * + 清乐观标记。对账失败必须可见：乐观视图与权威序列分叉会一直留存。
     */
    const SEGMENT_RECONCILE_DEBOUNCE_MS = 500
    const segmentReconcileTimers = new Map<string, ReturnType<typeof setTimeout>>()
    const handleSegmentActivated = (eventData: {
      data?: { thread_id?: string; pipeline_id?: string; segment_id?: string; _threadId?: string }
      _threadId?: string
    }) => {
      const pipelineId = eventData?.data?.pipeline_id || ''
      // 内核 emit_event 统一封装的会话坐标是 data._threadId（与流式族同口径，
      // 复用 extractThreadId 提取）；插件 frontend.emit 形态才携带 thread_id。
      const threadId = extractThreadId(eventData) || eventData?.data?.thread_id || ''
      if (!pipelineId || !threadId) {
        console.warn('[SEGMENT_ACTIVATED] 坐标缺失，跳过对账', eventData?.data)
        return
      }
      const pending = segmentReconcileTimers.get(pipelineId)
      if (pending) clearTimeout(pending)
      segmentReconcileTimers.set(
        pipelineId,
        setTimeout(() => {
          segmentReconcileTimers.delete(pipelineId)
          const ps = usePipelineMessageStore.getState()
          Promise.all([
            ps.fetchMessages(pipelineId, { threadId }),
            ps.fetchSegments(pipelineId),
          ])
            .then(() => {
              ps.clearSegmentView(pipelineId)
            })
            .catch(() => {
              useNotificationStore.getState().addNotification({
                title: '对话版本切换对账失败',
                message: '版本切换后消息同步失败，建议刷新页面查看当前启用的内容。',
                priority: 'normal',
                category: 'error',
                isBlocking: false,
                autoDismissMs: 8000,
                sourceLabel: '对话版本',
              })
            })
        }, SEGMENT_RECONCILE_DEBOUNCE_MS),
      )
    }
    globalWS.subscribe(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, handleSegmentActivated)

    /**
     * 被同账号新连接替换（B10 单连接踢旧，code=4000）：本页已永久失联且不再
     * 自动重连——必须明示用户，否则页面静默装死、消息全黑洞。典型成因：
     * 同一浏览器开了多个前端标签页互踢。
     */
    const handleKickedByReplacement = () => {
      useNotificationStore.getState().addNotification({
        title: '本页连接已被其他页面替换',
        message: '检测到同一账号在其他页面建立了新连接，本页已停止接收消息。请关闭多余页面，或刷新本页重新接管连接。',
        priority: 'high',
        category: 'error',
        isBlocking: false,
        autoDismissMs: 0, // 常驻：静默装死比打扰更糟
        sourceLabel: '前端',
      })
    }
    globalWS.subscribe(WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT, handleKickedByReplacement)

    // visibility 回前台主动重连：浏览器后台时节流 setInterval 心跳 + uvicorn ws_ping_timeout
    // 会掐断连接，但 onclose 可能在标签页冻结期间被延迟。回前台时主动检测：连接已断则重连，
    // 重连成功后 onopen 自动发 reconnected → 上方 handleWsReconnect 自动追新（fan-out 复用）。
    // 连接仍活着则不动（说明 WS 一直收消息，状态本就最新）。
    const handleVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return
      if (globalWS.status === 'connected') return
      // 被 4000 踢旧（本页已被其他连接替换）：不自动重连，避免互踢环
      // （connect() 内部同样拦截，此处显式短路 + 留痕便于排查）
      if (globalWS.wasKickedByReplacement()) {
        console.info('[useRealtimeEvents] 本页被新连接替换(code=4000)，回前台不自动重连（刷新页面可恢复）')
        return
      }
      // 「用前保证新鲜」（tokenLifecycle 唯一实现）：未过期直接返回当前 token；
      // 已过期先刷新，刷新失败返回 null——绝不用过期 token 硬连（4001 → 重连风暴），
      // 交由 GlobalWebSocket 既有重连机制退避处理（它有 isAuthFailureFromError
      // 判断，瞬时故障不登出）。
      void tokenLifecycle
        .ensureFreshToken()
        .then((token) => {
          if (token) {
            globalWS.connect(token)
          }
        })
        .catch(() => {
          // refresh 失败：不主动连，不登出，交给 GlobalWebSocket 既有重连兜底
        })
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      // WebSocket lifecycle
      globalWS.unsubscribe(WS_LOCAL_EVENTS.RECONNECTED, handleWsReconnect)
      document.removeEventListener('visibilitychange', handleVisibilityChange)

      // Task lifecycle events
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted)
      globalWS.unsubscribe(WS_LOCAL_EVENTS.USER_INPUT_SEND_TIMEOUT, handleUserInputSendTimeout)
      globalWS.unsubscribe(WS_SERVER_EVENTS.PENDING_INPUTS_CHANGED, handlePendingInputsChanged)
      globalWS.unsubscribe(WS_SERVER_EVENTS.COMPRESSION_FAILED, handleCompressionFailed)
    globalWS.unsubscribe(WS_SERVER_EVENTS.COMPRESSION_APPLIED, handleCompressionApplied)
    for (const timer of compressionReconcileTimers.values()) clearTimeout(timer)
    compressionReconcileTimers.clear()
    globalWS.unsubscribe(WS_SERVER_EVENTS.SEGMENT_ACTIVATED, handleSegmentActivated)
    for (const timer of segmentReconcileTimers.values()) clearTimeout(timer)
    segmentReconcileTimers.clear()
      globalWS.unsubscribe(WS_LOCAL_EVENTS.KICKED_BY_REPLACEMENT, handleKickedByReplacement)
    }
  }, [bumpWorkspaceDataVersion])
}
