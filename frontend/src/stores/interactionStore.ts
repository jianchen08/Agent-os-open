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

/** 制品（简化版，审批携带的 AI 产出物） */
export interface InteractionArtifact {
  id: string
  type: 'text' | 'image' | 'video' | 'audio' | 'screenshot' | 'file'
  content: string
  title?: string
  metadata?: Record<string, unknown>
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
  /** 制品列表（conversation 模式携带的 AI 产出物） */
  artifacts?: InteractionArtifact[]
  /** 关联的工作区 Tab ID（点击预览跳转用） */
  workspaceTabId?: string
  /** 优先级 */
  priority?: 'low' | 'normal' | 'high' | 'critical'
  /** 通知模式的进度百分比 (0-100) */
  progress?: number
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

  getPendingForThread: (threadId) => {
    return get().pendingInteractions.filter(
      (i) => i.threadId === threadId && i.status === 'pending',
    )
  },
}))
