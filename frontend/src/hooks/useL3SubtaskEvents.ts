/**
 * L3 子任务事件 Hook
 *
 * 订阅和处理 L3 子任务相关的 WebSocket 事件
 * 用于在 UI 中展示 L3 原子任务的执行进度
 */

import { useCallback, useEffect, useState } from 'react'
import { useWebSocket } from '@/hooks/useWebSocket'
import type {
  L3SubtaskCompletedEvent,
  L3SubtaskProgressEvent,
  L3SubtaskStartedEvent,
  L3SubtaskType,
} from '@/types/task'

/**
 * L3 子任务状态
 */
export interface L3SubtaskState {
  subtaskId: string
  subtaskType: L3SubtaskType
  name: string
  description?: string
  status: 'running' | 'completed' | 'failed'
  progress: number
  result?: Record<string, unknown>
  error?: string
  startTime: Date
  endTime?: Date
}

/**
 * useL3SubtaskEvents Hook 参数
 */
export interface UseL3SubtaskEventsOptions {
  /** 是否启用订阅（默认 true） */
  enabled?: boolean
  /** 父任务 ID（可选，用于过滤特定任务的子任务） */
  taskId?: string
}

/**
 * useL3SubtaskEvents Hook 返回值
 */
export interface UseL3SubtaskEventsReturn {
  /** 当前任务的 L3 子任务列表 */
  subtasks: L3SubtaskState[]
  /** 正在执行的子任务数量 */
  runningCount: number
  /** 已完成的子任务数量 */
  completedCount: number
  /** 失败的子任务数量 */
  failedCount: number
  /** 清除所有子任务状态 */
  clearSubtasks: () => void
}

/**
 * useL3SubtaskEvents Hook
 *
 * 订阅 L3 子任务事件，维护子任务状态列表
 *
 * @param options 配置选项
 * @returns L3 子任务状态和统计信息
 */
export function useL3SubtaskEvents(
  options: UseL3SubtaskEventsOptions = {},
): UseL3SubtaskEventsReturn {
  const { enabled = true, taskId } = options
  const { subscribe } = useWebSocket()

  // L3 子任务状态映射
  const [subtasksMap, setSubtasksMap] = useState<Map<string, L3SubtaskState>>(new Map())

  /**
   * 处理子任务开始事件
   */
  const handleSubtaskStarted = useCallback(
    (data: unknown) => {
      const event = data as L3SubtaskStartedEvent

      // 如果指定了 taskId，只处理匹配的任务
      if (taskId && event.taskId !== taskId) {
        return
      }

      setSubtasksMap((prev) => {
        const newMap = new Map(prev)
        newMap.set(event.subtaskId, {
          subtaskId: event.subtaskId,
          subtaskType: event.subtaskType,
          name: event.name,
          description: event.description,
          status: 'running',
          progress: 0,
          startTime: new Date(),
        })
        return newMap
      })

    },
    [taskId],
  )

  /**
   * 处理子任务进度事件
   */
  const handleSubtaskProgress = useCallback(
    (data: unknown) => {
      const event = data as L3SubtaskProgressEvent

      // 如果指定了 taskId，只处理匹配的任务
      if (taskId && event.taskId !== taskId) {
        return
      }

      setSubtasksMap((prev) => {
        const existing = prev.get(event.subtaskId)
        if (!existing) {
          return prev
        }

        const newMap = new Map(prev)
        newMap.set(event.subtaskId, {
          ...existing,
          progress: event.progress,
          description: event.message || existing.description,
        })
        return newMap
      })
    },
    [taskId],
  )

  /**
   * 处理子任务完成事件
   */
  const handleSubtaskCompleted = useCallback(
    (data: unknown) => {
      const event = data as L3SubtaskCompletedEvent

      // 如果指定了 taskId，只处理匹配的任务
      if (taskId && event.taskId !== taskId) {
        return
      }

      setSubtasksMap((prev) => {
        const existing = prev.get(event.subtaskId)
        if (!existing) {
          return prev
        }

        const newMap = new Map(prev)
        newMap.set(event.subtaskId, {
          ...existing,
          status: event.success ? 'completed' : 'failed',
          progress: event.success ? 100 : existing.progress,
          result: event.result,
          error: event.error,
          endTime: new Date(),
        })
        return newMap
      })

    },
    [taskId],
  )

  /**
   * 清除所有子任务状态
   */
  const clearSubtasks = useCallback(() => {
    setSubtasksMap(new Map())
  }, [])

  useEffect(() => {
    if (!enabled) {
      return
    }

    // 订阅 L3 子任务事件
    const unsubscribeStarted = subscribe('l3_subtask_started', handleSubtaskStarted)
    const unsubscribeProgress = subscribe('l3_subtask_progress', handleSubtaskProgress)
    const unsubscribeCompleted = subscribe('l3_subtask_completed', handleSubtaskCompleted)

    return () => {
      unsubscribeStarted()
      unsubscribeProgress()
      unsubscribeCompleted()
    }
  }, [enabled, subscribe, handleSubtaskStarted, handleSubtaskProgress, handleSubtaskCompleted])

  // 计算统计信息
  const subtasks = Array.from(subtasksMap.values())
  const runningCount = subtasks.filter((s) => s.status === 'running').length
  const completedCount = subtasks.filter((s) => s.status === 'completed').length
  const failedCount = subtasks.filter((s) => s.status === 'failed').length

  return {
    subtasks,
    runningCount,
    completedCount,
    failedCount,
    clearSubtasks,
  }
}

export default useL3SubtaskEvents
