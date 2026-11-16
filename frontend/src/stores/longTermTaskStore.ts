/** 长期任务状态管理 Store 基于 Task API 实现，替代废弃的 projectStore
 *
 * 服务端状态 query 化：tasks 数据容器已换 TanStack Query 缓存
 * （hooks/queries/useLongTermTasksQuery，queryKeys.longTermTasks）——
 * 列表拉取/5s 兜底轮询由 useLongTermTasksQuery（refetchInterval）承担，
 * WS 事件增量走 updateLongTermTasksCache / invalidateLongTermTasks。
 * 本 store 只承载 UI 选择态（activeTaskId）+ 写操作编排（API 调用 +
 * 乐观回填 query cache）。
 *
 * fetchTasks 已退役（原 5s 轮询调用方）；persist 收窄为仅 activeTaskId
 * （query 持久化已覆盖刷新秒开，见 queryPersister）。
 */

import { create } from 'zustand'
import { persist } from 'zustand/middleware'
import { updateLongTermTasksCache } from '@/hooks/queries/useLongTermTasksQuery'
import * as longTermTaskApi from '@/services/api/longTermTasks'
import { createTolerantStorage } from '@/utils/tolerantStorage'
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

/** 长期任务 Store 状态接口（仅 UI 态；tasks 数据在 query cache） */
interface LongTermTaskState {
  /** 当前活跃的长期任务 ID */
  activeTaskId: string | null
  /** 行级写操作进行中的任务 ID 集合（pause/resume/cancel/toggle await 窗口）。
   *  UI 行级 pending（任务面板行按钮转圈）与确认弹窗 confirming 态统一从此
   *  消费——调用方不再各自造 useState；直连 API 的操作编排方用 setTaskActing
   *  显式标记。运行时态，不持久化。 */
  actingTaskIds: Set<string>
}

/** 长期任务 Store 操作接口 */
interface LongTermTaskActions {
  /** 切换自动执行开关 */
  toggleAutoExecute: (taskId: string, enabled: boolean) => Promise<void>
  /** 暂停长期任务 */
  pauseTask: (taskId: string) => Promise<void>
  /** 恢复长期任务 */
  resumeTask: (taskId: string) => Promise<void>
  /** 取消长期任务 */
  cancelTask: (taskId: string, reason?: string) => Promise<void>
  /** 标记/清除行级操作进行中（供直连 API 的操作编排方在 await 窗口置位；
   *  本 store 的写动作自管理，无需外部标记） */
  setTaskActing: (taskId: string, acting: boolean) => void
  /** 设置活跃任务 */
  setActiveTask: (taskId: string | null) => void
  /** 更新任务状态（用于 WebSocket 事件增量；写 query cache） */
  updateTask: (taskId: string, updates: Partial<Task>) => void
  /** 删除任务（写 query cache；同步清理 activeTaskId） */
  deleteTask: (taskId: string) => void
}

/** 账户数据初始片段（cacheBoundary 切户清账消费；与 persist partialize 字段同域，
 *  actingTaskIds 为运行时态随附重置——防切户后残留他户任务的 pending 标记） */
export const LONG_TERM_TASK_ACCOUNT_RESET = {
  activeTaskId: null,
  actingTaskIds: new Set<string>(),
}

/** 长期任务 Store */
export const useLongTermTaskStore = create<LongTermTaskState & LongTermTaskActions>()(
  persist(
    (set) => ({
      ...LONG_TERM_TASK_ACCOUNT_RESET,

      /** 标记/清除行级操作进行中（Set 每次新建引用，保证订阅方感知变更） */
      setTaskActing: (taskId: string, acting: boolean) => {
        set((state) => {
          const next = new Set(state.actingTaskIds)
          if (acting) next.add(taskId)
          else next.delete(taskId)
          return { actingTaskIds: next }
        })
      },

      /** 切换自动执行开关 */
      toggleAutoExecute: async (taskId: string, enabled: boolean) => {
        set((state) => ({ actingTaskIds: new Set(state.actingTaskIds).add(taskId) }))
        try {
          const updatedTask = await longTermTaskApi.toggleAutoExecute(taskId, enabled)
          updateLongTermTasksCache((prev) =>
            prev.map((task) => (task.id === taskId ? updatedTask : task)),
          )
        } catch (error) {
          throw new Error(getErrorMessage(error, '切换自动执行失败'))
        } finally {
          set((state) => {
            const next = new Set(state.actingTaskIds)
            next.delete(taskId)
            return { actingTaskIds: next }
          })
        }
      },

      /** 暂停长期任务 */
      pauseTask: async (taskId: string) => {
        set((state) => ({ actingTaskIds: new Set(state.actingTaskIds).add(taskId) }))
        try {
          const updatedTask = await longTermTaskApi.pauseLongTermTask(taskId)
          updateLongTermTasksCache((prev) =>
            prev.map((task) => (task.id === taskId ? updatedTask : task)),
          )
        } catch (error) {
          throw new Error(getErrorMessage(error, '暂停长期任务失败'))
        } finally {
          set((state) => {
            const next = new Set(state.actingTaskIds)
            next.delete(taskId)
            return { actingTaskIds: next }
          })
        }
      },

      /** 恢复长期任务 */
      resumeTask: async (taskId: string) => {
        set((state) => ({ actingTaskIds: new Set(state.actingTaskIds).add(taskId) }))
        try {
          const updatedTask = await longTermTaskApi.resumeLongTermTask(taskId)
          updateLongTermTasksCache((prev) =>
            prev.map((task) => (task.id === taskId ? updatedTask : task)),
          )
        } catch (error) {
          throw new Error(getErrorMessage(error, '恢复长期任务失败'))
        } finally {
          set((state) => {
            const next = new Set(state.actingTaskIds)
            next.delete(taskId)
            return { actingTaskIds: next }
          })
        }
      },

      /** 取消长期任务 */
      cancelTask: async (taskId: string, reason?: string) => {
        set((state) => ({ actingTaskIds: new Set(state.actingTaskIds).add(taskId) }))
        try {
          const responseData = await longTermTaskApi.cancelLongTermTask(taskId, reason)
          updateLongTermTasksCache((prev) =>
            prev.map((task) =>
              // 后端 cancel_task 落 TaskStatus.STOPPED（旧 cancelled 已并入 stopped）
              task.id === taskId ? { ...task, ...responseData, status: 'stopped' as const } : task
            ),
          )
        } catch (error) {
          throw new Error(getErrorMessage(error, '取消长期任务失败'))
        } finally {
          set((state) => {
            const next = new Set(state.actingTaskIds)
            next.delete(taskId)
            return { actingTaskIds: next }
          })
        }
      },

      /** 设置活跃任务 */
      setActiveTask: (taskId: string | null) => {
        set({ activeTaskId: taskId })
      },

      /** 更新任务状态（用于 WebSocket 事件增量更新，写 query cache） */
      updateTask: (taskId: string, updates: Partial<Task>) => {
        updateLongTermTasksCache((prev) =>
          prev.map((task) => (task.id === taskId ? { ...task, ...updates } : task)),
        )
      },

      /** 删除任务（写 query cache；活跃任务被删时清空选择态） */
      deleteTask: (taskId: string) => {
        updateLongTermTasksCache((prev) => prev.filter((task) => task.id !== taskId))
        set((state) => (state.activeTaskId === taskId ? { activeTaskId: null } : {}))
      },
    }),
    {
      name: 'long-term-task-storage',
      // 配额满时吞掉 QuotaExceededError，避免 updateTask/deleteTask 等 action 崩溃
      storage: createTolerantStorage(),
      // tasks 已迁 query 持久化（queryPersister），此处只持久化 UI 选择态
      partialize: (state) => ({
        activeTaskId: state.activeTaskId,
      }),
    },
  ),
)
