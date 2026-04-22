/**
 * 澄清事件 Hook
 *
 * 订阅和处理澄清相关的 WebSocket 事件
 * 当 Agent 需要用户补充信息时，显示通知并支持跳转到对应 Tab
 */

import { useCallback, useEffect } from 'react'
import { toast } from 'sonner'
import { useAgentTabStore } from '@/stores/agentTabStore'
import type { ClarificationNeededEvent } from '@/types/task'
import { useWebSocket } from '@/hooks/useWebSocket'

/**
 * useClarificationEvents Hook 参数
 */
export interface UseClarificationEventsOptions {
  /** 是否启用订阅（默认 true） */
  enabled?: boolean
}

/**
 * useClarificationEvents Hook
 *
 * 订阅澄清请求事件，显示通知并支持跳转到对应 Agent Tab
 *
 * @param options 配置选项
 *
 * @example
 * ```tsx
 * // 在 App 或布局组件中使用
 * useClarificationEvents()
 * ```
 */
export function useClarificationEvents(
  options: UseClarificationEventsOptions = {}
) {
  const { enabled = true } = options
  const { subscribe } = useWebSocket()
  // 保留 store 调用以避免未来需要时重新添加
  useAgentTabStore()

  /**
   * 处理澄清请求事件
   */
  const handleClarificationNeeded = useCallback(
    (data: unknown) => {
      const event = data as ClarificationNeededEvent

      // 构建通知消息
      const message = event.question
        ? `Agent: ${event.question}`
        : 'Agent 需要您补充一些信息'

      // 显示可点击的通知
      toast.info('需要澄清', {
        description: message,
        duration: 10000, // 10 秒
        action: {
          label: '查看详情',
          onClick: () => {
            // 待实现：跳转到澄清详情的逻辑
          },
        },
      })
    },
    []
  )

  useEffect(() => {
    if (!enabled) {
      return
    }

    // 订阅澄清请求事件
    const unsubscribe = subscribe(
      'clarification_needed',
      handleClarificationNeeded
    )

    return () => {
      unsubscribe()
    }
  }, [enabled, subscribe, handleClarificationNeeded])
}

export default useClarificationEvents
