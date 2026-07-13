/** 长期任务状态管理 Store 基于 Task API 实现，替代废弃的 projectStore */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { createTolerantStorage } from '@/utils/tolerantStorage'
import * as longTermTaskApi from '@/services/api/longTermTasks'
import type { Task } from '@/types/task'

/** API 错误响应类型 */
interface ApiErrorResponse {
  response?: {
    data?: {
      message?: string
    }
  }
  message?: string
}

/** 提取错误消息 */
function getErrorMessage(error: unknown, defaultMessage: string): string {
  if (error instanceof Error) {
    const apiError = error as ApiErrorResponse
    return apiError.response?.data?.message || error.message || defaultMessage
  }
  return defaultMessage
}

/** 长期任务 Store 状态接口 */
interface LongTermTaskState {
  /** 长期任务列表 */
  tasks: Task[]
  /** 当前活跃的长期任务 ID */
  activeTaskId: string | null
  /** 是否正在加载 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null
}

/** 长期任务 Store 操作接口 */
interface LongTermTaskActions {
  /** 获取长期任务列表 */
  fetchTasks: () => Promise<void>
  /** 切换自动执行开关 */
  toggleAutoExecute: (taskId: string, enabled: boolean) => Promise<void>
  /** 暂停长期任务 */
  pauseTask: (taskId: string) => Promise<void>
  /** 恢复长期任务 */
  resumeTask: (taskId: string) => Promise<void>
  /** 取消长期任务 */
  cancelTask: (taskId: string, reason?: string) => Promise<void>
  /** 设置活跃任务 */
  setActiveTask: (taskId: string | null) => void
  /** 更新任务状态 */
  updateTask: (taskId: string, updates: Partial<Task>) => void
  /** 删除任务 */
  deleteTask: (taskId: string) => void
  /** 清除错误 */
  clearError: () => void
}

/** 长期任务 Store */
export const useLongTermTaskStore = create<LongTermTaskState & LongTermTaskActions>()(
  persist(
    (set, get) => ({
      // 初始状态
      tasks: [],
      activeTaskId: null,
      isLoading: false,
      error: null,

      /** 获取长期任务列表 */
      fetchTasks: async () => {
        const state = get()
        if (state.isLoading) {
          return
        }

        set({ isLoading: true, error: null })

        try {
          const response = await longTermTaskApi.fetchLongTermTasks()

          set({
            tasks: response.items,
            isLoading: false,
          })
        } catch (error) {
          const errorMessage = getErrorMessage(error, '获取长期任务列表失败')
          set({
            isLoading: false,
            error: errorMessage,
          })
          throw new Error(errorMessage)
        }
      },

      /** 切换自动执行开关 */
      toggleAutoExecute: async (taskId: string, enabled: boolean) => {
        set({ error: null })

        try {
          const updatedTask = await longTermTaskApi.toggleAutoExecute(taskId, enabled)

          set((state) => ({
            tasks: state.tasks.map((task) => (task.id === taskId ? updatedTask : task)),
          }))
        } catch (error) {
          const errorMessage = getErrorMessage(error, '切换自动执行失败')
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /** 暂停长期任务 */
      pauseTask: async (taskId: string) => {
        set({ error: null })

        try {
          const updatedTask = await longTermTaskApi.pauseLongTermTask(taskId)

          set((state) => ({
            tasks: state.tasks.map((task) => (task.id === taskId ? updatedTask : task)),
          }))
        } catch (error) {
          const errorMessage = getErrorMessage(error, '暂停长期任务失败')
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /** 恢复长期任务 */
      resumeTask: async (taskId: string) => {
        set({ error: null })

        try {
          const updatedTask = await longTermTaskApi.resumeLongTermTask(taskId)

          set((state) => ({
            tasks: state.tasks.map((task) => (task.id === taskId ? updatedTask : task)),
          }))
        } catch (error) {
          const errorMessage = getErrorMessage(error, '恢复长期任务失败')
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /** 取消长期任务 */
      cancelTask: async (taskId: string, reason?: string) => {
        set({ error: null })

        try {
          const responseData = await longTermTaskApi.cancelLongTermTask(taskId, reason)

          set((state) => ({
            tasks: state.tasks.map((task) =>
              task.id === taskId ? { ...task, ...responseData, status: 'cancelled' as const } : task
            ),
          }))
        } catch (error) {
          const errorMessage = getErrorMessage(error, '取消长期任务失败')
          set({ error: errorMessage })
          throw new Error(errorMessage)
        }
      },

      /** 设置活跃任务 */
      setActiveTask: (taskId: string | null) => {
        set({ activeTaskId: taskId })
      },

      /** 更新任务状态（用于 WebSocket 事件更新） */
      updateTask: (taskId: string, updates: Partial<Task>) => {
        set((state) => ({
          tasks: state.tasks.map((task) => (task.id === taskId ? { ...task, ...updates } : task)),
        }))
      },

      /** 删除任务 */
      deleteTask: (taskId: string) => {
        set((state) => ({
          tasks: state.tasks.filter((task) => task.id !== taskId),
          activeTaskId: state.activeTaskId === taskId ? null : state.activeTaskId,
        }))
      },

      /** 清除错误信息 */
      clearError: () => {
        set({ error: null })
      },
    }),
    {
      name: 'long-term-task-storage',
      // 配额满时吞掉 QuotaExceededError，避免 updateTask/deleteTask 等 action 崩溃
      storage: createTolerantStorage(),
      partialize: (state) => ({
        tasks: state.tasks,
        activeTaskId: state.activeTaskId,
      }),
    },
  ),
)
