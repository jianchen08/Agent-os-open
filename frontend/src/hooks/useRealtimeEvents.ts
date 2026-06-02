/**
 * useRealtimeEvents Hook
 *
 * 订阅实时 WebSocket 事件并路由到 layout mode store 进行展示。
 *
 * 处理：
 * - 执行进度：EXECUTION_START / EXECUTION_PROGRESS / EXECUTION_DONE / EXECUTION_CANCELLED
 * - Sub-agent 事件：SUB_AGENT_CREATED / SUB_AGENT_COMPLETED / SUB_AGENT_WAITING_INPUT
 *   （registerPipelineTab 由 streamingEventService 处理；执行追踪在此）
 * - 任务生命周期：TASK_STATUS_UPDATE
 * - 模块 Schema 更新：SCHEMA_UPDATED
 *
 * 注意：stream_start / stream_chunk / stream_end / stream_error 等流式事件
 *       已由 streaming/index.ts 的 initStreamingEvents 统一处理，此处不重复订阅。
 */

import { useEffect } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { handleSchemaUpdate } from '@/services/modules'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { generateUUID } from '@/utils/uuid'
import type { ExecutionEvent, InteractionRequest } from '@/stores/layoutModeStore'

/**
 * Hook to subscribe to real-time WebSocket events and update the layout store.
 *
 * Call once in a top-level component (e.g. FiveSpaceHomePage).
 */
export function useRealtimeEvents(): void {
  const addOrUpdateExecution = useLayoutModeStore((s) => s.addOrUpdateExecution)
  const removeExecution = useLayoutModeStore((s) => s.removeExecution)
  const addInteraction = useLayoutModeStore((s) => s.addInteraction)
  const updateConnectionStatus = useLayoutModeStore((s) => s.updateConnectionStatus)
  const bumpWorkspaceDataVersion = useLayoutModeStore((s) => s.bumpWorkspaceDataVersion)

  useEffect(() => {
    // 防抖用时间戳记录，跟踪上次 fetchMessages 调用时间
    const lastFetchTimeRef = { current: 0 }

    /**
     * FIX: WS 重连后重新加载当前会话消息，1 秒防抖避免频繁调用。
     * 流式事件（stream_start 等）由 streaming/index.ts 统一处理，此处不重复订阅。
     *
     * BUG-FIX-fix_20260601_ws_connect_fetch:
     * 问题根因: 页面刷新后 isStreaming 为 false，导致不会调用 fetchMessages 获取最新消息。
     *          即使后端仍在流式输出，前端也无法接收到新消息。
     * 修复方案: 移除 isStreaming 检查，WS 重连后总是调用 fetchMessages 获取最新消息（包括可能正在流式的消息）。
     *          streaming/index.ts 中的 handleReconnected 会处理流式状态的恢复和补漏。
     * 影响范围: 页面刷新后、WS 重连后的消息获取
     * 修复日期: 2026-06-01
     */
    const handleWsConnect = () => {
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId } = useSessionStore.getState()
      // FIX: 用 activePipelineId 而非 sessionId 调 fetchMessages
      const activePipelineId = usePipelineMessageStore.getState().activePipelineId
      if (activeSessionId && activePipelineId) {
        // 总是获取最新消息，不管是否正在流式输出
        // handleReconnected 会处理流式状态的恢复
        usePipelineMessageStore.getState().fetchMessages(activePipelineId, { threadId: activeSessionId }).catch(() => {})
      }
    }

    // ---- Execution progress handlers ----

    const handleExecutionStart = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const event: ExecutionEvent = {
        id: (data.execution_id as string) || generateUUID(),
        type: (data.execution_type as ExecutionEvent['type']) || 'tool',
        name: (data.name as string) || 'Unknown',
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

    // ---- Sub-agent event handlers ----

    // FIX: 所有 handler 统一从 data.data 解包实际数据
    const handleSubAgentCreated = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const agentId = (data.agentId as string) || (data.taskId as string)
      const agentName = (data.agentName as string) || 'Sub-agent'
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

    // ---- Task lifecycle handlers ----

    // FIX: 订阅 task_status_update，触发工作区刷新并更新 longTermTaskStore 中的任务状态
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

    // ---- Subscribe to all events ----

    // WebSocket lifecycle
    globalWS.subscribe('connect', handleWsConnect)

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
      globalWS.unsubscribe('connect', handleWsConnect)

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
