/**
 * 执行卡片状态管理 Store
 *
 * 管理实时执行卡片的状态，处理 WebSocket 执行事件
 * 与 useExecutionRecord Hook 配合使用：
 * - 实时执行：通过此 Store 管理
 * - 历史记录：通过 useExecutionRecord 查询 API
 *
 * @module executionCardStore
 */

import type {
    ExecutionCardData,
    ExecutionDoneEvent,
    ExecutionEvent,
    ExecutionProgressEvent,
    ExecutionStartEvent,
} from '@/types/execution'
import { mergeExecutionEvent } from '@/types/execution'
import { create } from 'zustand'

/**
 * 执行卡片 Store 状态接口
 */
interface ExecutionCardState {
  /** 所有执行卡片数据（按 ID 索引） */
  cards: Map<string, ExecutionCardData>

  /** 处理执行开始事件 */
  handleStart: (event: ExecutionStartEvent) => void

  /** 处理执行进度事件 */
  handleProgress: (event: ExecutionProgressEvent) => void

  /** 处理执行完成事件 */
  handleDone: (event: ExecutionDoneEvent) => void

  /** 统一事件处理入口 */
  handleEvent: (event: ExecutionEvent) => void

  /** 获取指定 ID 的卡片 */
  getCard: (id: string) => ExecutionCardData | undefined

  /** 获取指定会话的所有卡片 */
  getSessionCards: (sessionId: string) => ExecutionCardData[]

  /** 获取正在执行的卡片 */
  getRunningCards: () => ExecutionCardData[]

  /** 清除指定会话的卡片 */
  clearSessionCards: (sessionId: string) => void

  /** 清除所有卡片 */
  clearAll: () => void

  /** 更新卡片数据 */
  updateCard: (id: string, data: Partial<ExecutionCardData>) => void
}

/**
 * 执行卡片 Store
 */
export const useExecutionCardStore = create<ExecutionCardState>()((set, get) => ({
  cards: new Map(),

  handleStart: (event: ExecutionStartEvent) => {
    console.log('[executionCardStore] 处理执行开始事件:', event.executionId, event.name)
    set((state) => {
      const next = new Map(state.cards)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return { cards: next }
    })
  },

  handleProgress: (event: ExecutionProgressEvent) => {
    set((state) => {
      const next = new Map(state.cards)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return { cards: next }
    })
  },

  handleDone: (event: ExecutionDoneEvent) => {
    console.log('[executionCardStore] 处理执行完成事件:', event.executionId, event.success)
    set((state) => {
      const next = new Map(state.cards)
      const existing = next.get(event.executionId)
      next.set(event.executionId, mergeExecutionEvent(existing, event))
      return { cards: next }
    })
  },

  handleEvent: (event: ExecutionEvent) => {
    const { handleStart, handleProgress, handleDone } = get()
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

  getCard: (id: string) => {
    return get().cards.get(id)
  },

  getSessionCards: (sessionId: string) => {
    const cards = get().cards
    return Array.from(cards.values()).filter(
      (card) => card.metadata?.sessionId === sessionId
    )
  },

  getRunningCards: () => {
    const cards = get().cards
    return Array.from(cards.values()).filter(
      (card) => card.status === 'running'
    )
  },

  clearSessionCards: (sessionId: string) => {
    set((state) => {
      const next = new Map(state.cards)
      for (const [id, card] of next) {
        if (card.metadata?.sessionId === sessionId) {
          next.delete(id)
        }
      }
      return { cards: next }
    })
  },

  clearAll: () => {
    set({ cards: new Map() })
  },

  updateCard: (id: string, data: Partial<ExecutionCardData>) => {
    set((state) => {
      const next = new Map(state.cards)
      const existing = next.get(id)
      if (existing) {
        next.set(id, { ...existing, ...data })
      }
      return { cards: next }
    })
  },
}))

export default useExecutionCardStore
