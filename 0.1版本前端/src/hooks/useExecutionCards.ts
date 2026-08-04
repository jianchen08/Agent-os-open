/**
 * 执行卡片状态管理 Hook
 *
 * 处理 execution_start, execution_progress, execution_done 事件
 * 合并事件数据为统一的卡片状态
 */

import { useCallback, useMemo, useState } from 'react'
import { mergeExecutionEvent } from '@/types/execution'
import type {
  ExecutionCardData,
  ExecutionDoneEvent,
  ExecutionEvent,
  ExecutionProgressEvent,
  ExecutionStartEvent,
} from '@/types/execution'

/**
 * Hook 返回值类型
 */
export interface UseExecutionCardsReturn {
  /** 所有执行卡片数据（按 ID 索引） */
  cards: Map<string, ExecutionCardData>
  /** 执行卡片数组（按开始时间排序） */
  cardList: ExecutionCardData[]
  /** 处理执行事件 */
  handleEvent: (event: ExecutionEvent) => void
  /** 处理执行开始事件 */
  handleStart: (event: ExecutionStartEvent) => void
  /** 处理执行进度事件 */
  handleProgress: (event: ExecutionProgressEvent) => void
  /** 处理执行完成事件 */
  handleDone: (event: ExecutionDoneEvent) => void
  /** 获取指定 ID 的卡片 */
  getCard: (id: string) => ExecutionCardData | undefined
  /** 获取指定父 ID 的子卡片 */
  getChildCards: (parentId: string) => ExecutionCardData[]
  /** 清除所有卡片 */
  clearAll: () => void
  /** 清除指定 ID 的卡片 */
  clearCard: (id: string) => void
  /** 正在执行的卡片数量 */
  runningCount: number
}

/**
 * 执行卡片状态管理 Hook
 */
export function useExecutionCards(): UseExecutionCardsReturn {
  const [cards, setCards] = useState<Map<string, ExecutionCardData>>(new Map())

  /**
   * 处理执行开始事件
   */
  const handleStart = useCallback((event: ExecutionStartEvent) => {
    setCards((prev) => {
      const next = new Map(prev)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return next
    })
  }, [])

  /**
   * 处理执行进度事件
   */
  const handleProgress = useCallback((event: ExecutionProgressEvent) => {
    setCards((prev) => {
      const next = new Map(prev)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return next
    })
  }, [])

  /**
   * 处理执行完成事件
   */
  const handleDone = useCallback((event: ExecutionDoneEvent) => {
    setCards((prev) => {
      const next = new Map(prev)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return next
    })
  }, [])

  /**
   * 统一事件处理入口
   */
  const handleEvent = useCallback(
    (event: ExecutionEvent) => {
      switch (event.type) {
        case 'execution_start':
          handleStart(event)
          break
        case 'execution_progress':
          handleProgress(event)
          break
        case 'execution_done':
          handleDone(event)
          break
      }
    },
    [handleStart, handleProgress, handleDone],
  )

  /**
   * 获取指定 ID 的卡片
   */
  const getCard = useCallback(
    (id: string) => {
      return cards.get(id)
    },
    [cards],
  )

  /**
   * 获取指定父 ID 的子卡片
   */
  const getChildCards = useCallback(
    (parentId: string) => {
      return Array.from(cards.values()).filter((card) => card.parentId === parentId)
    },
    [cards],
  )

  /**
   * 清除所有卡片
   */
  const clearAll = useCallback(() => {
    setCards(new Map())
  }, [])

  /**
   * 清除指定 ID 的卡片
   */
  const clearCard = useCallback((id: string) => {
    setCards((prev) => {
      const next = new Map(prev)
      next.delete(id)
      return next
    })
  }, [])

  /**
   * 卡片列表（按开始时间排序）
   */
  const cardList = useMemo(() => {
    return Array.from(cards.values()).sort((a, b) => {
      const timeA = a.startTime ? new Date(a.startTime).getTime() : 0
      const timeB = b.startTime ? new Date(b.startTime).getTime() : 0
      return timeA - timeB
    })
  }, [cards])

  /**
   * 正在执行的卡片数量
   */
  const runningCount = useMemo(() => {
    return Array.from(cards.values()).filter((card) => card.status === 'running').length
  }, [cards])

  return {
    cards,
    cardList,
    handleEvent,
    handleStart,
    handleProgress,
    handleDone,
    getCard,
    getChildCards,
    clearAll,
    clearCard,
    runningCount,
  }
}

export default useExecutionCards
