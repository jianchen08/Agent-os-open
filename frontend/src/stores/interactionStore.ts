/**
 * Interaction Store
 *
 * 管理人类交互请求的状态。纯状态层，不涉及通信或 UI。
 */

import { create } from 'zustand'

/**
 * 交互优先级权重映射
 * 数值越大优先级越高，用于排序计算
 */
const PRIORITY_WEIGHT: Record<string, number> = {
  critical: 4,
  high: 3,
  normal: 2,
  low: 1,
}

/** 交互选项 */
export interface InteractionOption {
  id: string
  label: string
  description?: string
}

/** 待处理交互 */
export interface PendingInteraction {
  requestId: string
  mode: 'choice' | 'conversation' | 'notification'
  title: string
  description: string
  threadId: string
  tabId: string
  agentId: string
  /** pipeline_id，用于流式消息路由到对应子 Tab */
  pipelineId?: string
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
  /** 通知模式的进度百分比 (0-100) */
  progress?: number
  timestamp: string
  status: 'pending' | 'responded' | 'navigated' | 'dismissed' | 'entered'
  /** 审批请求 ID（仅 conversation 模式下审批场景有值） */
  reviewRequestId?: string
  /** 关联制品 ID 列表（仅审批场景有值） */
  artifactIds?: string[]
  /** 文件内容映射（由前端通过 file-content API 拉取） */
  fileContents?: Record<string, string>
  /** 所属会话 ID */
  sessionId?: string
}

interface InteractionState {
  /** 待处理交互列表 */
  pendingInteractions: PendingInteraction[]
  /** 全局浮层中打开的交互请求 ID */
  globalOpenRequestId: string | null
  /** 全局浮层是否最小化 */
  isMinimized: boolean

  /** 添加待处理交互 */
  addInteraction: (data: Omit<PendingInteraction, 'status'>) => void
  /** 标记已响应 */
  markResponded: (requestId: string) => void
  /** 标记已跳转到子标签 */
  markNavigated: (requestId: string) => void
  /** 取消/忽略 */
  dismissInteraction: (requestId: string) => void
  /** 标记用户已进入对话（管道挂起等待用户输入） */
  markEntered: (requestId: string) => void
  /** 按 threadId 获取待处理交互 */
  getPendingForThread: (threadId: string) => PendingInteraction[]
  /** 按 pipelineId 获取已进入但未响应的交互 */
  getEnteredForPipeline: (pipelineId: string) => PendingInteraction | undefined
  /** 打开全局交互浮层 */
  setGlobalOpenRequestId: (id: string | null) => void
  /** 切换最小化状态 */
  toggleMinimized: () => void
  /** 设置最小化状态 */
  setMinimized: (minimized: boolean) => void
}

export const useInteractionStore = create<InteractionState>()((set, get) => ({
  pendingInteractions: [],
  globalOpenRequestId: null,
  isMinimized: false,

  addInteraction: (data) => {
    set((state) => {
      const existing = state.pendingInteractions.find(
        (i) => i.requestId === data.requestId,
      )
      if (existing) return state

      /** 将新交互插入列表后按优先级权重降序排列，相同优先级按时间升序排列 */
      const updated = [
        ...state.pendingInteractions,
        { ...data, status: 'pending' as const },
      ]
      updated.sort((a, b) => {
        const weightDiff =
          (PRIORITY_WEIGHT[b.priority ?? 'normal'] ?? 2) -
          (PRIORITY_WEIGHT[a.priority ?? 'normal'] ?? 2)
        if (weightDiff !== 0) return weightDiff
        return new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime()
      })

      return { pendingInteractions: updated }
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

  markEntered: (requestId) => {
    set((state) => {
      const existing = state.pendingInteractions.find((i) => i.requestId === requestId)
      if (!existing || existing.status === 'entered') return state

      return {
        pendingInteractions: state.pendingInteractions.map((i) =>
          i.requestId === requestId ? { ...i, status: 'entered' as const } : i,
        ),
      }
    })
  },

  getPendingForThread: (threadId) => {
    return get().pendingInteractions.filter(
      (i) => i.threadId === threadId && i.status === 'pending',
    )
  },

  getEnteredForPipeline: (pipelineId) => {
    return get().pendingInteractions.find(
      (i) =>
        i.status === 'entered' &&
        (i.pipelineId === pipelineId ||
          i.threadId === pipelineId ||
          i.agentId === pipelineId),
    )
  },

  setGlobalOpenRequestId: (id) => {
    set({ globalOpenRequestId: id })
  },

  toggleMinimized: () => {
    set((state) => ({ isMinimized: !state.isMinimized }))
  },

  setMinimized: (minimized) => {
    set({ isMinimized: minimized })
  },
}))
