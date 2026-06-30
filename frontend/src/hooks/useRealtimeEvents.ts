/** useRealtimeEvents Hook 订阅实时 WebSocket 事件并路由到 layout mode store 进行展示。 */

import { useEffect } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { handleSchemaUpdate } from '@/services/modules'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { generateUUID } from '@/utils/uuid'
import type { ExecutionEvent, InteractionRequest } from '@/stores/layoutModeStore'

/** Hook to subscribe to real-time WebSocket events and update the layout store. Call once in a top-level component (e.g. FiveSpaceHomePage). */
export function useRealtimeEvents(): void {
  const addOrUpdateExecution = useLayoutModeStore((s) => s.addOrUpdateExecution)
  const removeExecution = useLayoutModeStore((s) => s.removeExecution)
  const addInteraction = useLayoutModeStore((s) => s.addInteraction)
  const updateConnectionStatus = useLayoutModeStore((s) => s.updateConnectionStatus)
  const bumpWorkspaceDataVersion = useLayoutModeStore((s) => s.bumpWorkspaceDataVersion)

  useEffect(() => {
    // 防抖用时间戳记录，跟踪上次 fetchMessages 调用时间
    const lastFetchTimeRef = { current: 0 }

    /** WS 重连后重新加载当前会话消息，1 秒防抖避免频繁调用。 流式事件（stream_start 等）由 streaming/index.ts 统一处理，此处不重复订阅。 */
    const handleWsReconnect = () => {
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId } = useSessionStore.getState()
      // 双游标补漏：WS 重连后，对所有有 bottomCursor 的管道走 after_sequence 增量补漏，
      // 只拉断线期间缺失的新消息，天然不重复。延后一帧执行（外层 requestAnimationFrame），
      // 确保 lifecycleHandlers 的 handleReconnected 先清理完残留 streaming 占位。
      const store = usePipelineMessageStore.getState()
      const pipelinesToBackfill: string[] = []
      for (const [pid, cursor] of Object.entries(store.bottomCursorsByPipeline)) {
        if (cursor > 0) pipelinesToBackfill.push(pid)
      }
      // 统一加载入口：WS 重连必须无条件补漏（skipStreamingCheck=true，刷新后 isStreaming=false），
      // 强制 mode='backfill' 走 after_sequence 增量。失败时通知用户。
      for (const pid of pipelinesToBackfill) {
        usePipelineMessageStore
          .getState()
          .loadPipelineMessages(pid, {
            threadId: activeSessionId || '',
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

      // 直到后端推送 SCHEMA_UPDATED 事件或重新登录才恢复。
      import('@/services/modules/ModuleManager')
        .then(({ moduleManager }) => moduleManager.syncOnReconnect())
        .catch(() => {
          // syncOnReconnect 内部已兜底，此处仅防止未捕获 rejection
        })
    }

    // Execution progress handlers

    const handleExecutionStart = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const execId = data.execution_id as string | undefined
      const execName = data.name as string | undefined
      if (!execId) {
        console.warn('[useRealtimeEvents] execution_id 缺失，无法追踪执行事件', data)
      }
      const event: ExecutionEvent = {
        id: execId || generateUUID(),
        type: (data.execution_type as ExecutionEvent['type']) || 'tool',
        name: execName || 'Unknown',
        status: 'running',
        progress: 0,
        startedAt: new Date().toISOString(),
      }
      addOrUpdateExecution(event)
    }

    const handleExecutionProgress = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const existingExecutions = useLayoutModeStore.getState().activeExecutions
      const executionId = data.execution_id as string
      const existing = existingExecutions.find((e) => e.id === executionId)

      if (existing) {
        addOrUpdateExecution({
          ...existing,
          progress: (data.progress as number) ?? existing.progress,
        })
      }
    }

    const handleExecutionOutput = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const existingExecutions = useLayoutModeStore.getState().activeExecutions
      const executionId = data.execution_id as string
      const existing = existingExecutions.find((e) => e.id === executionId)

      if (existing) {
        const newOutput = data.append
          ? (existing.output || '') + (data.output as string)
          : (data.output as string)
        addOrUpdateExecution({
          ...existing,
          output: newOutput,
        })
      }
    }

    const handleExecutionDone = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const existingExecutions = useLayoutModeStore.getState().activeExecutions
      const executionId = data.execution_id as string
      const existing = existingExecutions.find((e) => e.id === executionId)

      if (existing) {
        addOrUpdateExecution({
          ...existing,
          status: (data.success as boolean) ? 'completed' : 'failed',
          progress: 100,
          completedAt: new Date().toISOString(),
          error: (data.error as string) || undefined,
        })

        setTimeout(() => {
          removeExecution(executionId)
        }, 10000)
      }

      bumpWorkspaceDataVersion()
    }

    const handleExecutionCancelled = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const existingExecutions = useLayoutModeStore.getState().activeExecutions
      const executionId = data.execution_id as string
      const existing = existingExecutions.find((e) => e.id === executionId)

      if (existing) {
        addOrUpdateExecution({
          ...existing,
          status: 'cancelled',
          completedAt: new Date().toISOString(),
        })

        setTimeout(() => {
          removeExecution(executionId)
        }, 5000)
      }
    }

    // Interaction events are handled by useInteractionHandler hook

    // Sub-agent event handlers

    // 所有 handler 统一从 data.data 解包实际数据
    const handleSubAgentCreated = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const agentId = (data.agentId as string) || (data.taskId as string)
      const agentName = (data.agentName as string) || 'Sub-agent'
      if (!agentId) {
        console.warn('[useRealtimeEvents] Sub-agent agentId 缺失，无法追踪', data)
      }
      const event: ExecutionEvent = {
        id: `agent-${agentId || 'unknown'}`,
        type: 'agent',
        name: agentName,
        status: 'running',
        progress: 0,
        startedAt: new Date().toISOString(),
      }
      addOrUpdateExecution(event)

      // Note: registerPipelineTab is handled by streamingEventService.ts
      bumpWorkspaceDataVersion()
    }

    const handleSubAgentWaitingInput = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const agentId = data.agentId as string
      const request: InteractionRequest = {
        id: `interaction-agent-${agentId}`,
        executionId: agentId,
        prompt: (data.prompt as string) || `${data.agentName as string || 'Agent'} is waiting for input`,
        timestamp: new Date().toISOString(),
      }
      addInteraction(request)
    }

    const handleSubAgentCompleted = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const agentId = data.agentId as string
      const existingExecutions = useLayoutModeStore.getState().activeExecutions
      const existing = existingExecutions.find((e) => e.id === `agent-${agentId}`)

      if (existing) {
        addOrUpdateExecution({
          ...existing,
          status: (data.success as boolean) ? 'completed' : 'failed',
          progress: 100,
          completedAt: new Date().toISOString(),
        })

        setTimeout(() => {
          removeExecution(`agent-${agentId}`)
        }, 10000)
      }

      bumpWorkspaceDataVersion()
    }

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

    // Execution events
    globalWS.subscribe(WS_SERVER_EVENTS.EXECUTION_START, handleExecutionStart as any)
    globalWS.subscribe(WS_SERVER_EVENTS.EXECUTION_PROGRESS, handleExecutionProgress as any)
    globalWS.subscribe(WS_SERVER_EVENTS.EXECUTION_OUTPUT, handleExecutionOutput as any)
    globalWS.subscribe(WS_SERVER_EVENTS.EXECUTION_DONE, handleExecutionDone as any)
    globalWS.subscribe(WS_SERVER_EVENTS.EXECUTION_CANCELLED, handleExecutionCancelled as any)

    // Sub-agent events
    globalWS.subscribe(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated as any)
    globalWS.subscribe(
      WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT,
      handleSubAgentWaitingInput as any,
    )
    globalWS.subscribe(WS_SERVER_EVENTS.SUB_AGENT_COMPLETED, handleSubAgentCompleted as any)

    // Task lifecycle events
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate as any)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged as any)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted as any)

    // Module schema update events (event-driven, replaces polling)
    const handleSchemaUpdatedEvent = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      handleSchemaUpdate({
        module_id: (data.module_id as string) || '',
        schema_version: (data.schema_version as string) || '',
        changes: (data.changes as string[]) || [],
      })
    }
    globalWS.subscribe(WS_SERVER_EVENTS.SCHEMA_UPDATED, handleSchemaUpdatedEvent as any)

    return () => {
      // WebSocket lifecycle
      globalWS.unsubscribe('reconnected', handleWsReconnect)

      // Execution events
      globalWS.unsubscribe(WS_SERVER_EVENTS.EXECUTION_START, handleExecutionStart as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.EXECUTION_PROGRESS, handleExecutionProgress as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.EXECUTION_OUTPUT, handleExecutionOutput as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.EXECUTION_DONE, handleExecutionDone as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.EXECUTION_CANCELLED, handleExecutionCancelled as any)

      // Sub-agent events
      globalWS.unsubscribe(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated as any)
      globalWS.unsubscribe(
        WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT,
        handleSubAgentWaitingInput as any,
      )
      globalWS.unsubscribe(WS_SERVER_EVENTS.SUB_AGENT_COMPLETED, handleSubAgentCompleted as any)

      // Task lifecycle events
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleTaskStatusUpdate as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleTaskStatusChanged as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_DELETED, handleTaskDeleted as any)

      // Module schema events
      globalWS.unsubscribe(WS_SERVER_EVENTS.SCHEMA_UPDATED, handleSchemaUpdatedEvent as any)
    }
  }, [
    addOrUpdateExecution,
    removeExecution,
    addInteraction,
    updateConnectionStatus,
    bumpWorkspaceDataVersion,
  ])
}
