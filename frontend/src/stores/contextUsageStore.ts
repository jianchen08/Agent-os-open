/**
 * 上下文使用量 Store
 *
 * 保存每个管道（pipeline）最新的 usage 数据，供 ChatInput 的进度条显示使用。
 * 数据来源：stream_end WebSocket 事件中的 usage 字段。
 * 每个管道独立维护自己的 usage、模型名和 context_window。
 */

import { create } from 'zustand'

interface PipelineUsage {
  /** prompt token 数（即上下文 token 使用量） */
  promptTokens: number
  /** completion token 数 */
  completionTokens: number
  /** 总 token 数 */
  totalTokens: number
}

interface ContextUsageState {
  /** 各管道最新的 usage 数据（key 为 pipelineId） */
  usageByPipeline: Record<string, PipelineUsage>
  /** 更新某个管道的 usage 数据 */
  updateUsage: (pipelineId: string, usage: Record<string, number>) => void
  /** 获取某个管道的 prompt tokens（上下文使用量） */
  getPromptTokens: (pipelineId: string) => number
  /** 获取某个管道的完整 usage 数据 */
  getUsage: (pipelineId: string) => PipelineUsage | undefined
  /** 清除某个管道的 usage 数据 */
  clearUsage: (pipelineId: string) => void
}

export const useContextUsageStore = create<ContextUsageState>((set, get) => ({
  usageByPipeline: {},

  updateUsage: (pipelineId, usage) => {
    set((state) => ({
      usageByPipeline: {
        ...state.usageByPipeline,
        [pipelineId]: {
          promptTokens: usage.prompt_tokens || usage.input_tokens || 0,
          completionTokens: usage.completion_tokens || usage.output_tokens || 0,
          totalTokens: usage.total_tokens || 0,
        },
      },
    }))
  },

  getPromptTokens: (pipelineId) => {
    return get().usageByPipeline[pipelineId]?.promptTokens ?? 0
  },

  getUsage: (pipelineId) => {
    return get().usageByPipeline[pipelineId]
  },

  clearUsage: (pipelineId) => {
    set((state) => {
      const next = { ...state.usageByPipeline }
      delete next[pipelineId]
      return { usageByPipeline: next }
    })
  },
}))
