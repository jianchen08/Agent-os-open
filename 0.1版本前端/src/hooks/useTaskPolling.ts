/** useTaskPolling Hook 定期轮询长期任务状态，作为 WebSocket 实时事件（useRealtimeEvents）的补充 fallback。 */

import { useEffect, useRef, useCallback } from 'react'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 任务终态集合 */
const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled', 'timeout'] as const

/** 判断任务状态是否为终态。 */
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

/** 任务状态轮询 Hook。 定期从服务端拉取任务列表，保持本地 store 数据新鲜。 */
export function useTaskPolling(options: UseTaskPollingOptions = {}): void {
  const { interval = 5000, enabled = true } = options

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

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

    /** 单次轮询 tick */
    const tick = () => {
      // 页面不可见时跳过本次轮询
      if (document.hidden) {
        return
      }

      const store = useLongTermTaskStore.getState()
      store.fetchTasks().catch((error) => {
        useNotificationStore.getState().addNotification({
          title: '任务同步失败',
          message: error instanceof Error ? error.message : '无法同步长期任务状态，请稍后重试',
          priority: 'normal',
          category: 'error',
          isBlocking: false,
          autoDismissMs: 5000,
        })
      })
    }

    // 启动定时轮询
    timerRef.current = setInterval(tick, interval)

    // 组件卸载时清理定时器
    return () => {
      clearTimer()
    }
  }, [enabled, interval, clearTimer])
}
