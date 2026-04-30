/**
 * Interaction Store
 *
 * 管理人类交互请求的状态。纯状态层，不涉及通信或 UI。
 */

import { create } from 'zustand'

/** 交互选项 */
export interface InteractionOption {
  id: string
  label: string
}

/** 待处理交互 */
export interface PendingInteraction {
  requestId: string
  mode: 'choice' | 'conversation'
  title: string
  description: string
  threadId: string
  tabId: string
  agentId: string
  /** 选择模式的选项 */
  options?: InteractionOption[]
  /** 澄清问题 */
  questions?: string[]
  /** 对话模式的开场消息 */
  initialMessage?: string
  /** 快捷回复建议 */
  suggestions?: string[]
  /** 优先级 */
  priority?: 'low' | 'normal' | 'high' | 'critical'
  timestamp: string
  status: 'pending' | 'responded' | 'navigated' | 'dismissed'
}

interface InteractionState {
  /** 待处理交互列表 */
  pendingInteractions: PendingInteraction[]

  /** 添加待处理交互 */
  addInteraction: (data: Omit<PendingInteraction, 'status'>) => void
  /** 标记已响应 */
  markResponded: (requestId: string) => void
  /** 标记已跳转到子标签 */
  markNavigated: (requestId: string) => void
  /** 取消/忽略 */
  dismissInteraction: (requestId: string) => void
  /** 按 threadId 获取待处理交互 */
  getPendingForThread: (threadId: string) => PendingInteraction[]
}

export const useInteractionStore = create<InteractionState>()((set, get) => ({
  pendingInteractions: [],

  addInteraction: (data) => {
    set((state) => {
      const existing = state.pendingInteractions.find(
        (i) => i.requestId === data.requestId,
      )
      if (existing) return state

      return {
        pendingInteractions: [
          ...state.pendingInteractions,
          { ...data, status: 'pending' as const },
        ],
      }
    })
  },

  markResponded: (requestId) => {
    set((state) => {
      const existing = state.pendingInteractions.find((i) => i.requestId === requestId)
      if (!existing || existing.status === 'responded') return state

      return {
        pendingInteractions: state.pendingInteractions.map((i) =>
          i.requestId === requestId ? { ...i, status: 'responded' as const } : i,
        ),
      }
    })
  },

  markNavigated: (requestId) => {
    set((state) => {
      const existing = state.pendingInteractions.find((i) => i.requestId === requestId)
      if (!existing || existing.status === 'navigated') return state

      return {
        pendingInteractions: state.pendingInteractions.map((i) =>
          i.requestId === requestId ? { ...i, status: 'navigated' as const } : i,
        ),
      }
    })
  },

  dismissInteraction: (requestId) => {
    set((state) => {
      const exists = state.pendingInteractions.some((i) => i.requestId === requestId)
      if (!exists) return state

      return {
        pendingInteractions: state.pendingInteractions.filter(
          (i) => i.requestId !== requestId,
        ),
      }
    })
  },

  getPendingForThread: (threadId) => {
    return get().pendingInteractions.filter(
      (i) => i.threadId === threadId && i.status === 'pending',
    )
  },
}))
