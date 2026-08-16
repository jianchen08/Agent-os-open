/** useRealtimeEvents Hook 订阅实时 WebSocket 事件并路由到 layout mode store 进行展示。 */

import { useEffect } from 'react'
import { useAuthStore } from '@/stores/authStore'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { usePipelineRegistryStore } from '@/stores/pipelineRegistryStore'
import { useSessionStore } from '@/stores/sessionStore'

/** Hook to subscribe to real-time WebSocket events and update the layout store. Call once in a top-level component (e.g. FiveSpaceHomePage). */
export function useRealtimeEvents(): void {
  const bumpWorkspaceDataVersion = useLayoutModeStore((s) => s.bumpWorkspaceDataVersion)

  useEffect(() => {
    // 防抖用时间戳记录，跟踪上次 fetchMessages 调用时间
    const lastFetchTimeRef = { current: 0 }

    /** WS 重连后重新加载当前会话消息，1 秒防抖避免频繁调用。 流式事件（stream_start 等）由 streaming/index.ts 统一处理，此处不重复订阅。 */
    const handleWsReconnect = () => {
      // 重连后补拉管道运行快照（断线期间的 stream_* 增量丢失，以快照对账）
      usePipelineRegistryStore.getState().fetch()
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId, sessions } = useSessionStore.getState()
      if (!activeSessionId) return
      // 只补当前会话的【主管道】，不对 session.pipelineIds 全部扇出。
      // 子管道的消息在用户切到对应 tab 时按需加载。
      const session = sessions.find((s) => s.id === activeSessionId)
      const mainPipelineId = session?.pipelineIds?.[0]
      if (!mainPipelineId) return

      // 走 backfill（增量补漏，走后端尾部读优化）而非 init（全量加载）。
      // init 触发 initFromAPI → 后端 _list_by_pipeline_full 全量读大 YAML（4.3MB+，
      // 单请求 10-40s），多个 pipeline 并发即雪崩。backfill 走 read_records_from_tail
      // 尾部窗口读，秒级返回。流式占位的 id 对账问题由 ensureStreamingPlaceholder 的
      // 状态守护保证安全，无需靠 init 全量覆盖。
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

    // 订阅 task_status_update，触发工作区刷新并更新 longTermTaskStore 中的任务状态
    const handleTaskStatusUpdate = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const taskId = (data.task_id || data.taskId) as string | undefined
      const newStatus = data.new_status as string | undefined
      const currentPhase = data.current_phase as string | undefined

      if (taskId && newStatus) {
        const store = useLongTermTaskStore.getState()
        const exists = store.tasks.some((t: Record<string, unknown>) => t.id === taskId)
        if (exists) {
          const updates: Record<string, unknown> = { status: newStatus }
          if (currentPhase) {
            updates.currentPhase = currentPhase
          }
          const errorMsg = data.error as string | undefined
          if (errorMsg) {
            updates.error = errorMsg
          }
          store.updateTask(taskId, updates as never)
        } else {
          store.fetchTasks().catch(() => {})
        }
      }

      bumpWorkspaceDataVersion()
    }

    const handleTaskDeleted = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const taskId = (data.task_id || data.taskId) as string | undefined

      if (taskId) {
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
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate as any)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged as any)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted as any)

    // visibility 回前台主动重连：浏览器后台时节流 setInterval 心跳 + uvicorn ws_ping_timeout
    // 会掐断连接，但 onclose 可能在标签页冻结期间被延迟。回前台时主动检测：连接已断则重连，
    // 重连成功后 onopen 自动发 reconnected → 上方 handleWsReconnect 自动追新（fan-out 复用）。
    // 连接仍活着则不动（说明 WS 一直收消息，状态本就最新）。
    const handleVisibilityChange = () => {
      if (document.visibilityState !== 'visible') return
      if (globalWS.status === 'connected') return
      const { checkTokenExpiration, refreshToken, token } = useAuthStore.getState()
      // token 过期：先刷新再连。必须用 .then()（成功才连），绝不能用 .finally()——
      // refresh 失败时若仍用旧过期 token 硬连，会触发 4001 → 重连链 → 可能误登出。
      // refresh 失败时不 connect，让 GlobalWebSocket 既有重连机制自行处理（它有
      // isAuthFailureFromError 判断，瞬时故障不登出）。
      if (checkTokenExpiration()) {
        refreshToken()
          .then(() => globalWS.connect(useAuthStore.getState().token || ''))
          .catch(() => {
            // refresh 失败：不主动连，不登出，交给 GlobalWebSocket 既有重连兜底
          })
      } else {
        globalWS.connect(token || '')
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)

    return () => {
      // WebSocket lifecycle
      globalWS.unsubscribe('reconnected', handleWsReconnect)
      document.removeEventListener('visibilitychange', handleVisibilityChange)

      // Task lifecycle events
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted as any)
    }
  }, [bumpWorkspaceDataVersion])
}
