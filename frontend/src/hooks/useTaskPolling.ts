/** useTaskPolling Hook 定期轮询长期任务状态，作为 WebSocket 实时事件（useRealtimeEvents）的补充 fallback。 */

import { useEffect, useRef, useCallback } from 'react'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'
import { loggers } from '@/utils/logger'

const logger = loggers.taskPolling

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
      // 兜底轮询静默失败：本 hook 是 WebSocket 实时链路的降级补充，连接状态已由
      // useConnectionStatus / useRealtimeEvents 统一提示。单次拉取失败若弹用户可见
      // 通知，持续性故障（启动时序竞态、代理未就绪等）会每 5s 刷屏。错误仍写入
      // store.error 供 UI 按需读取，并打到控制台便于排查真实原因。
      store.fetchTasks().catch((error) => {
        logger.warn(
          '长期任务兜底轮询失败:',
          error instanceof Error ? error.message : error,
        )
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
