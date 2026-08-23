/**
 * Agent 选择态 store（服务端状态 query 化批次 1 瘦身）
 *
 * agents 列表数据已迁 TanStack Query（hooks/queries/useAgentsQuery，
 * 统一走 apiClient 传输层），本 store 只保留当前选中 agent（纯 UI 状态）。
 * 历史字段 agents/isLoading/error/fetchAgents/clearError 已随迁移退役。
 */

import { create } from 'zustand'

interface AgentState {
  /** 当前选择的 Agent ID（null 表示使用默认助手） */
  currentAgentId: string | null

  /** 设置当前 Agent */
  setCurrentAgentId: (agentId: string | null) => void
}

export const useAgentStore = create<AgentState>((set) => ({
  currentAgentId: null,

  setCurrentAgentId: (agentId: string | null) => {
    set({ currentAgentId: agentId })
  },
}))
