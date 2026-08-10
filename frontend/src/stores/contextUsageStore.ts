/**
 * 上下文使用量 Store
 *
 * 保存每个管道（pipeline）最新的 usage 数据，供 ChatInput 的进度条显示使用。
 * 数据来源：cost_update WebSocket 事件（track 插件每轮 llm_call 后推送）。
 * 每个管道独立维护自己的 usage、模型名和 context_window。
 *
 * 每个管道的 usage 含两套数据：
 * - 单轮值（promptTokens/completionTokens/totalTokens）：表达当前上下文窗口
 *   占用，进度条据此计算占窗比。
 * - cumulative（命中/未命中/输出分别加总）：整个管道跨轮累计消耗，统计区据此
 *   显示「缓存命中输入 / 未命中输入 / 输出 分别加总」。
 */

import { create } from 'zustand'

/** 累计 token 用量（整个管道跨轮加总） */
export interface CumulativeUsage {
  /** 累计输入 token（含缓存命中） */
  inputTokens: number
  /** 累计输出 token */
  outputTokens: number
  /** 累计缓存命中输入 token */
  cachedTokens: number
  /** 累计未命中输入 token（= inputTokens - cachedTokens，下界 0） */
  missedTokens: number
  /** 累计总额 token */
  totalTokens: number
}

interface PipelineUsage {
  /** prompt token 数（即上下文 token 使用量，单轮） */
  promptTokens: number
  /** completion token 数（单轮） */
  completionTokens: number
  /** 总 token 数（单轮） */
  totalTokens: number
  /** 整个管道的累计 token 用量（跨轮加总） */
  cumulative?: CumulativeUsage
}

interface ContextUsageState {
  /** 各管道最新的 usage 数据（key 为 pipelineId） */
  usageByPipeline: Record<string, PipelineUsage>
  /** 更新某个管道的 usage 数据 */
  updateUsage: (pipelineId: string, usage: Record<string, number> & { cumulative?: Record<string, number> }) => void
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
    set((state) => {
      const prev = state.usageByPipeline[pipelineId]
      // cumulative 可能缺失（旧后端 / 异常轮），保留上一轮累计值避免统计闪烁
      const rawCum = usage.cumulative
      const cumulative = rawCum
        ? {
            inputTokens: rawCum.input_tokens ?? rawCum.inputTokens ?? 0,
            outputTokens: rawCum.output_tokens ?? rawCum.outputTokens ?? 0,
            cachedTokens: rawCum.cached_tokens ?? rawCum.cachedTokens ?? 0,
            missedTokens: rawCum.missed_tokens ?? rawCum.missedTokens ?? 0,
            totalTokens: rawCum.total_tokens ?? rawCum.totalTokens ?? 0,
          }
        : prev?.cumulative
      return {
        usageByPipeline: {
          ...state.usageByPipeline,
          [pipelineId]: {
            promptTokens: usage.prompt_tokens || usage.input_tokens || 0,
            completionTokens: usage.completion_tokens || usage.output_tokens || 0,
            totalTokens: usage.total_tokens || 0,
            cumulative,
          },
        },
      }
    })
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
