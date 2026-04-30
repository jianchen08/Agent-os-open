/**
 * 任务事件 Hook
 *
 * 订阅和处理任务相关的 WebSocket 事件
 * 监听 execution_start/execution_done 事件，更新 longTermTaskStore 中的任务状态
 */

import { useEffect } from 'react'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { useWebSocket } from '@/hooks/useWebSocket'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'

/**
 * useTaskEvents Hook 参数
 */
export interface UseTaskEventsOptions {
  /** 是否启用订阅（默认 true） */
  enabled?: boolean
  /** 会话 ID（可选，用于过滤事件） */
  sessionId?: string
  /** 任务 ID（可选，用于过滤特定任务的事件） */
  taskId?: string
}

/**
 * useTaskEvents Hook
 *
 * 订阅任务相关的 WebSocket 事件，实时更新任务状态
 *
 * @param options 配置选项
 */
export function useTaskEvents(options: UseTaskEventsOptions = {}) {
  const { enabled = true } = options
  const { subscribe } = useWebSocket()
  const updateTask = useLongTermTaskStore((state) => state.updateTask)
  const fetchTasks = useLongTermTaskStore((state) => state.fetchTasks)

  useEffect(() => {
    if (!enabled) {
      return
    }

    const unsubscribers: (() => void)[] = []

    /**
     * 处理执行开始事件
     */
    const handleExecutionStart = (data: unknown) => {
      const event = data as Record<string, unknown>
      const executionId = (event.execution_id || event.executionId) as string
      if (!executionId) return

      const metadata = event.metadata as Record<string, unknown> | undefined
      const taskId = metadata?.task_id as string | undefined
      if (taskId) {
        updateTask(taskId, { status: 'running' } as never)
      }
    }

    /**
     * 处理执行完成事件
     */
    const handleExecutionDone = (data: unknown) => {
      const event = data as Record<string, unknown>
      const executionId = (event.execution_id || event.executionId) as string
      if (!executionId) return

      const success = event.success as boolean | undefined
      const metadata = event.metadata as Record<string, unknown> | undefined
      const taskId = metadata?.task_id as string | undefined

      if (taskId) {
        updateTask(taskId, {
          status: success ? 'completed' : 'failed',
        } as never)
      }

      fetchTasks().catch(() => {})
    }

    unsubscribers.push(subscribe(WS_SERVER_EVENTS.EXECUTION_START, handleExecutionStart))
    unsubscribers.push(subscribe(WS_SERVER_EVENTS.EXECUTION_DONE, handleExecutionDone))

    return () => {
      unsubscribers.forEach((unsub) => unsub())
    }
  }, [enabled, subscribe, updateTask, fetchTasks])
}

/**
 * useProjectEvents Hook
 *
 * 订阅长期任务相关的 WebSocket 事件
 *
 * @param options 配置选项
 */
export function useProjectEvents(options: { enabled?: boolean } = {}) {
  const { enabled = true } = options
  const { subscribe } = useWebSocket()
  const updateTask = useLongTermTaskStore((state) => state.updateTask)

  useEffect(() => {
    if (!enabled) {
      return
    }

    const unsubscribers: (() => void)[] = []

    /**
     * 处理任务完成事件
     */
    const handleTaskCompleted = (data: unknown) => {
      const event = data as Record<string, unknown>
      const taskId = (event.task_id || event.taskId) as string
      if (taskId) {
        updateTask(taskId, { status: 'completed' } as never)
      }
    }

    /**
     * 处理任务失败/取消事件
     */
    const handleTaskFailed = (data: unknown) => {
      const event = data as Record<string, unknown>
      const taskId = (event.task_id || event.taskId) as string
      if (taskId) {
        updateTask(taskId, { status: 'failed' } as never)
      }
    }

    unsubscribers.push(subscribe(WS_SERVER_EVENTS.TASK_COMPLETED, handleTaskCompleted))
    unsubscribers.push(subscribe(WS_SERVER_EVENTS.TASK_CANCELLED, handleTaskFailed))

    return () => {
      unsubscribers.forEach((unsub) => unsub())
    }
  }, [enabled, subscribe, updateTask])
}
