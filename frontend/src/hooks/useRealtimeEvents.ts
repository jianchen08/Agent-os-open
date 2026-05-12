/**
 * useRealtimeEvents Hook
 *
 * Subscribes to real-time WebSocket events and routes them into
 * the layout mode store for display in the five-space layout.
 *
 * Handles:
 * - Streaming output: STREAM_START (connection status only; chunks/end/error handled by streamingEventService)
 * - Execution progress: EXECUTION_START / EXECUTION_PROGRESS / EXECUTION_DONE / EXECUTION_CANCELLED
 * - Sub-agent events: SUB_AGENT_CREATED / SUB_AGENT_COMPLETED / SUB_AGENT_WAITING_INPUT
 *   (registerPipelineTab handled by streamingEventService; execution tracking here)
 * - Task lifecycle: TASK_STATUS_UPDATE
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

    // ---- Streaming output handlers ----

    const handleStreamStart = (data: Record<string, unknown>) => {
      // Streaming started - connection is confirmed working
      updateConnectionStatus({ state: 'connected', lastConnectedAt: new Date().toISOString() })
    }

    /**
     * BUG-FIX-fix_20260507_ws_reconnect_refresh:
     * 问题根因: WebSocket 重连后，任务树和消息列表不会自动刷新，
     *          导致断线期间产生的新任务和消息无法显示。
     * 修复方案: 监听 WebSocket connect 事件（包括重连），重新加载当前会话消息，
     *          并触发 layoutModeStore 的 workspaceTabs 更新（间接刷新任务树）。
     *
     * BUG-FIX-fix_20260512_ws_fetch_debounce:
     * 问题根因: WebSocket 重连时短时间内多次触发 connect 事件，
     *          导致 fetchMessages 被频繁调用，触发服务端 429 限流。
     * 修复方案: 添加 1 秒防抖，短时间内重复调用直接跳过。
     */
    const handleWsConnect = () => {
      // 防抖：1 秒内不重复调用 fetchMessages
      const now = Date.now()
      if (now - lastFetchTimeRef.current < 1000) {
        return
      }
      lastFetchTimeRef.current = now

      const { activeSessionId } = useSessionStore.getState()
      if (activeSessionId) {
        const isStreaming = usePipelineMessageStore.getState().isStreaming(activeSessionId)
        if (!isStreaming) {
          usePipelineMessageStore.getState().fetchMessages(activeSessionId).catch(() => {})
        }
      }
    }

    // Note: stream_chunk, stream_end, stream_error are handled by streamingEventService.ts

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

    // BUG-FIX-fix_20260510_event_data_unwrap:
    // 问题根因: wsPool.emitGlobal 将后端事件展开为 { data: {...}, _threadId },
    //           但 handler 直接从顶层读取字段，导致 agentId/pipelineId 等字段全部为 undefined。
    //           这导致子管道 pipelineId 映射未建立，tool_start 等事件无法路由到正确的子 Tab，
    //           工具调用不显示，子标签名显示为 "Sub-agent"（乱码/默认值）。
    // 修复方案: 所有 handler 统一从 data.data 解包实际数据。
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

    // BUG-FIX-fix_20260511_subagent_workspace_refresh:
    // 问题根因: 子 agent 提交任务后，任务工作区不自动刷新。
    //   后端 task_status_update 事件覆盖所有任务状态变更（pending/running/completed/failed），
    //   但前端 useTaskEvents 从未被挂载，无人处理该事件，bumpWorkspaceDataVersion 不会被调用。
    // 修复方案: 在 useRealtimeEvents 中订阅 task_status_update，触发工作区刷新。
    // BUG-FIX-fix_20260511_task_status_not_update:
    // 问题根因: handleTaskStatusUpdate 只刷新了工作区版本号，未更新 longTermTaskStore 中的
    //   任务状态，导致任务列表 UI 不刷新。同时 rawData 中 task_id/new_status 嵌套在 data 字段下。
    // 修复方案: 解包 rawData.data，调用 updateTask 更新 store 中的任务状态。
    const handleTaskStatusUpdate = (rawData: Record<string, unknown>) => {
      const data = (rawData.data as Record<string, unknown>) || rawData
      const taskId = (data.task_id || data.taskId) as string | undefined
      const newStatus = data.new_status as string | undefined

      if (taskId && newStatus) {
        useLongTermTaskStore.getState().updateTask(taskId, { status: newStatus } as never)
      }

      bumpWorkspaceDataVersion()
    }

    // ---- Subscribe to all events ----

    // WebSocket lifecycle
    globalWS.subscribe('connect', handleWsConnect)

    // Streaming events
    globalWS.subscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart as any)

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

      // Streaming events
      globalWS.unsubscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart as any)

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
