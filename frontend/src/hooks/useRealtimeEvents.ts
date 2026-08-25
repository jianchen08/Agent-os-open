/** useRealtimeEvents Hook 订阅实时 WebSocket 事件并路由到 layout mode store 进行展示。 */

import { useEffect } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import * as tokenLifecycle from '@/services/auth/tokenLifecycle'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { mainPipelineIdOf } from '@/utils/mappers'
import { useSessionStore } from '@/stores/sessionStore'
import { readSessions } from '@/hooks/queries/useSessionsQuery'
import {
  readLongTermTasks,
  updateLongTermTasksCache,
  invalidateLongTermTasks,
} from '@/hooks/queries/useLongTermTasksQuery'
import {
  invalidatePipelineRuns,
  invalidatePipelineStates,
} from '@/hooks/queries/usePipelineRunsQuery'

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
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId } = useSessionStore.getState()
      const sessions = readSessions()
      if (!activeSessionId) return
      // 只补当前会话的【主管道】（权威 activePipelineId 解析），
      // 不对 session.pipelineIds 全部扇出。
      // 子管道的消息在用户切到对应 tab 时按需加载。
      const session = sessions.find((s) => s.id === activeSessionId)
      const mainPipelineId = session ? mainPipelineIdOf(session) : undefined
      if (!mainPipelineId) return

      // 走 backfill（after_sequence 尾部游标读）而非 init（全量替换）：
      // 0.2 消息在 SQLite（message_slots+blobs），游标分页已下推 SQL——backfill
      // 是 O(增量窗口) 的索引查询，重连补漏秒级；init 全量替换会丢弃刷新前
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
    globalWS.subscribe('reconnected', handleWsReconnect)

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
      })
    }
    globalWS.subscribe('user_input_send_timeout', handleUserInputSendTimeout)

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
      })
    }
    globalWS.subscribe('kicked_by_replacement', handleKickedByReplacement)

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
      globalWS.unsubscribe('reconnected', handleWsReconnect)
      document.removeEventListener('visibilitychange', handleVisibilityChange)

      // Task lifecycle events
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted)
      globalWS.unsubscribe('user_input_send_timeout', handleUserInputSendTimeout)
      globalWS.unsubscribe('kicked_by_replacement', handleKickedByReplacement)
    }
  }, [bumpWorkspaceDataVersion])
}
