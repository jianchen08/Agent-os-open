/**
 * 终止评估 Store（task_observability 1c）
 *
 * 数据源：termination_advisor Input 插件每轮经 frontend.emit 推送的
 * termination_status WS 事件（handleTerminationStatus 写入）。
 * 消费方：CostDashboardWidget「剩余预算 + 收敛信号」指示器。
 */

import { create } from 'zustand'

/** 收敛信号（插件 termination_advisor.status.convergence 透传） */
export type ConvergenceSignal = 'converging' | 'stalled' | 'budget_critical'

/** 单管道终止评估状态 */
export interface TerminationStatus {
  /** 收敛信号 */
  convergence: ConvergenceSignal | string
  /** 是否已判定应终止 */
  shouldStop: boolean
  /** 终止原因（命中时非空） */
  stopReason: string
  /** 剩余预算百分比（预算信号缺失时 null → 前端显示「未启用」） */
  remainingBudgetPercent: number | null
  /** 当前轮次 */
  iteration: number
  /** 累计耗时（秒） */
  elapsedS: number
  /** 事件到达时间戳（ms） */
  ts: number
}

interface TerminationState {
  /** 各管道最新终止评估（key 为 pipelineId） */
  statusByPipeline: Record<string, TerminationStatus>
  /** 写入某管道的终止评估 */
  updateStatus: (pipelineId: string, status: Omit<TerminationStatus, 'ts'>) => void
  /** 读取某管道的终止评估 */
  getStatus: (pipelineId: string) => TerminationStatus | undefined
  /** 清除某管道的评估 */
  clearStatus: (pipelineId: string) => void
}

export const useTerminationStore = create<TerminationState>((set, get) => ({
  statusByPipeline: {},

  updateStatus: (pipelineId, status) => {
    set((state) => ({
      statusByPipeline: {
        ...state.statusByPipeline,
        [pipelineId]: { ...status, ts: Date.now() },
      },
    }))
  },

  getStatus: (pipelineId) => get().statusByPipeline[pipelineId],

  clearStatus: (pipelineId) => {
    set((state) => {
      const next = { ...state.statusByPipeline }
      delete next[pipelineId]
      return { statusByPipeline: next }
    })
  },
}))
