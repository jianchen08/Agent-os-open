/**
 * useRealtimeEvents Hook
 *
 * Subscribes to all real-time WebSocket events and routes them into
 * the layout mode store for display in the five-space layout.
 *
 * Handles:
 * - Streaming output: STREAM_START / STREAM_CHUNK / STREAM_END
 * - Execution progress: EXECUTION_START / EXECUTION_PROGRESS / EXECUTION_DONE / EXECUTION_CANCELLED
 * - Sub-agent events: SUB_AGENT_CREATED / SUB_AGENT_COMPLETED / SUB_AGENT_WAITING_INPUT
 */

import { useEffect } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { webSocketService } from '@/services/websocket/WebSocketService'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
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
  const removeInteraction = useLayoutModeStore((s) => s.removeInteraction)
  const updateConnectionStatus = useLayoutModeStore((s) => s.updateConnectionStatus)

  useEffect(() => {
    // ---- Streaming output handlers ----

    const handleStreamStart = (data: Record<string, unknown>) => {
      // Streaming started - connection is confirmed working
      updateConnectionStatus({ state: 'connected', lastConnectedAt: new Date().toISOString() })
    }

    const handleStreamChunk = (_data: Record<string, unknown>) => {
      // Chunks are handled by the session store already
    }

    const handleStreamEnd = (_data: Record<string, unknown>) => {
      // Stream complete
    }

    const handleStreamError = (_data: Record<string, unknown>) => {
      // Stream error - still connected but had an error
    }

    // ---- Execution progress handlers ----

    const handleExecutionStart = (data: Record<string, unknown>) => {
      const event: ExecutionEvent = {
        id: (data.execution_id as string) || crypto.randomUUID(),
        type: (data.execution_type as ExecutionEvent['type']) || 'tool',
        name: (data.name as string) || 'Unknown',
        status: 'running',
        progress: 0,
        startedAt: new Date().toISOString(),
      }
      addOrUpdateExecution(event)
    }

    const handleExecutionProgress = (data: Record<string, unknown>) => {
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

    const handleExecutionOutput = (data: Record<string, unknown>) => {
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

    const handleExecutionDone = (data: Record<string, unknown>) => {
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

        // Auto-remove completed executions after 10 seconds
        setTimeout(() => {
          removeExecution(executionId)
        }, 10000)
      }
    }

    const handleExecutionCancelled = (data: Record<string, unknown>) => {
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

    const handleSubAgentCreated = (data: Record<string, unknown>) => {
      const event: ExecutionEvent = {
        id: `agent-${data.agentId as string}`,
        type: 'agent',
        name: (data.agentName as string) || 'Sub-agent',
        status: 'running',
        progress: 0,
        startedAt: new Date().toISOString(),
      }
      addOrUpdateExecution(event)

      // BUG-FIX-fix_auto_pop_sub_tab:
      // 问题根因: sub_agent_created 事件自动调用 openSubAgentTab 导致子标签强制弹出，
      //           用户尚未点击任务就被切换到子标签页，体验不佳。
      // 修复方案: 不再自动打开子标签，仅注册 pipeline_id → tabId 映射，
      //           子标签在以下场景才弹出：
      //           1. 用户点击任务树节点（FiveSpaceLayout.handleTaskNodeClick）
      //           2. 人类交互进入 conversation 模式（useInteractionHandler）
      // 影响范围: 任务提交后的子标签创建流程
      // 修复日期: 2026-05-06
      const taskId = (data.taskId as string) || (data.agentId as string)
      const pipelineId = data.pipelineId as string | undefined
      const parentId = data.parentId as string | undefined

      if (taskId && pipelineId) {
        const tabId = `sub-${parentId || taskId}`
        useAgentTabStore.getState().registerPipelineTab(pipelineId, tabId)
      }
    }

    const handleSubAgentWaitingInput = (data: Record<string, unknown>) => {
      const agentId = data.agentId as string
      const request: InteractionRequest = {
        id: `interaction-agent-${agentId}`,
        executionId: agentId,
        prompt: (data.prompt as string) || `${data.agentName as string} is waiting for input`,
        timestamp: new Date().toISOString(),
      }
      addInteraction(request)
    }

    const handleSubAgentCompleted = (data: Record<string, unknown>) => {
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
    }

    // ---- Workflow step handlers ----

    const handleWorkflowStepUpdate = (data: Record<string, unknown>) => {
      // Could be used for more granular progress display
    }

    // ---- Subscribe to all events ----

    // Streaming events
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError as any)

    // Execution events
    webSocketService.subscribe(WS_SERVER_EVENTS.EXECUTION_START, handleExecutionStart as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.EXECUTION_PROGRESS, handleExecutionProgress as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.EXECUTION_OUTPUT, handleExecutionOutput as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.EXECUTION_DONE, handleExecutionDone as any)
    webSocketService.subscribe(WS_SERVER_EVENTS.EXECUTION_CANCELLED, handleExecutionCancelled as any)

    // Sub-agent events
    webSocketService.subscribe(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated as any)
    webSocketService.subscribe(
      WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT,
      handleSubAgentWaitingInput as any,
    )
    webSocketService.subscribe(WS_SERVER_EVENTS.SUB_AGENT_COMPLETED, handleSubAgentCompleted as any)

    // Workflow events
    webSocketService.subscribe(
      WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE,
      handleWorkflowStepUpdate as any,
    )

    return () => {
      // Streaming events
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_START, handleStreamStart as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_CHUNK, handleStreamChunk as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_END, handleStreamEnd as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.STREAM_ERROR, handleStreamError as any)

      // Execution events
      webSocketService.unsubscribe(WS_SERVER_EVENTS.EXECUTION_START, handleExecutionStart as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.EXECUTION_PROGRESS, handleExecutionProgress as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.EXECUTION_OUTPUT, handleExecutionOutput as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.EXECUTION_DONE, handleExecutionDone as any)
      webSocketService.unsubscribe(WS_SERVER_EVENTS.EXECUTION_CANCELLED, handleExecutionCancelled as any)

      // Sub-agent events
      webSocketService.unsubscribe(WS_SERVER_EVENTS.SUB_AGENT_CREATED, handleSubAgentCreated as any)
      webSocketService.unsubscribe(
        WS_SERVER_EVENTS.SUB_AGENT_WAITING_INPUT,
        handleSubAgentWaitingInput as any,
      )
      webSocketService.unsubscribe(WS_SERVER_EVENTS.SUB_AGENT_COMPLETED, handleSubAgentCompleted as any)

      // Workflow events
      webSocketService.unsubscribe(
        WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE,
        handleWorkflowStepUpdate as any,
      )
    }
  }, [
    addOrUpdateExecution,
    removeExecution,
    addInteraction,
    removeInteraction,
    updateConnectionStatus,
  ])
}
