/**
 * useTaskPolling Hook
 *
 * 定期轮询长期任务状态，作为 WebSocket 实时事件（useRealtimeEvents）的补充 fallback。
 *
 * 核心行为：
 * - 使用 setInterval 定期调用 longTermTaskStore.fetchTasks() 刷新任务列表
 * - 轮询间隔默认 5 秒
 * - 所有任务进入终态（completed/failed/cancelled/timeout）时自动停止轮询
 * - enabled 参数控制是否启动
 * - 组件卸载时清理定时器，防止内存泄漏
 * - 页面不可见（document.hidden）时暂停轮询
 */

import { useEffect, useRef, useCallback } from 'react'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 任务终态集合 */
const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'timeout'] as const

/**
 * 判断任务状态是否为终态。
 *
 * @param status - 任务状态字符串
 * @returns 是否为终态
 */
export function isTerminalTask(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status)
}

/** useTaskPolling 配置选项 */
export interface UseTaskPollingOptions {
  /** 轮询间隔（毫秒），默认 5000 */
  interval?: number
  /** 是否启用轮询，默认 true */
  enabled?: boolean
}

/**
 * 任务状态轮询 Hook。
 *
 * 定期从服务端拉取任务列表，保持本地 store 数据新鲜。
 * 当所有任务均已进入终态时自动停止轮询以节省资源。
 *
 * @param options - 配置选项
 *
 * @example
 * ```tsx
 * // 基础用法：默认 5 秒轮询
 * useTaskPolling()
 *
 * // 自定义间隔 + 条件启用
 * useTaskPolling({ interval: 3000, enabled: hasActiveTasks })
 * ```
 */
export function useTaskPolling(options: UseTaskPollingOptions = {}): void {
  const { interval = 5000, enabled = true } = options

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const stoppedRef = useRef(false)

  /** 清除定时器 */
  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      return
    }

    // 重置停止标记（enabled 或任务变化后重新评估）
    stoppedRef.current = false

    /** 单次轮询 tick */
    const tick = () => {
      // 页面不可见时跳过本次轮询
      if (document.hidden) {
        return
      }

      // 已停止则不再执行
      if (stoppedRef.current) {
        return
      }

      const store = useLongTermTaskStore.getState()
      store.fetchTasks().catch((error) => {
        // BUG-FIX-fix_20260617_silent_polling_catch:
        // 问题根因: 原代码 .catch(() => {}) 静默吞异常，用户无法感知任务轮询失败。
        // 修复方案: 通过 notification store 通知用户任务同步失败。
        // fetchTasks 内部已设置 store.error 状态，此处仅补充视觉通知。
        useNotificationStore.getState().addNotification({
          title: '任务同步失败',
          message: error instanceof Error ? error.message : '无法同步长期任务状态，请稍后重试',
          priority: 'normal',
          category: 'error',
          isBlocking: false,
          autoDismissMs: 5000,
        })
      })

      // 获取最新的任务列表，判断是否需要停止
      const tasks = useLongTermTaskStore.getState().tasks

      // 任务列表不为空且所有任务均处于终态 → 自动停止轮询
      if (tasks.length > 0 && tasks.every((task) => isTerminalTask(task.status))) {
        stoppedRef.current = true
        clearTimer()
      }
    }

    // 启动定时轮询
    timerRef.current = setInterval(tick, interval)

    // 组件卸载时清理定时器
    return () => {
      clearTimer()
    }
  }, [enabled, interval, clearTimer])
}
