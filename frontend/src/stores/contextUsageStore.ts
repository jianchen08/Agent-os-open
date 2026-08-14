/**
 * 上下文使用量 Store
 *
 * 保存每个管道（pipeline）最新的 usage 数据，供 ChatInput 的进度条显示使用。
 * 数据来源：stream_end WebSocket 事件中的 usage 字段 + cost_update 事件
 * （track 插件推送的单轮值/累计值，task_observability 1a/1b）。
 * 每个管道独立维护自己的 usage、模型名和 context_window。
 *
 * cache 维度（task_observability 1b）：cachedTokens/missedTokens/hitRatio 为
 * 本轮单轮值；cumulative 为管道累计；cacheHistory 为会话级命中率趋势
 * （内存态，每轮一条，封顶 200 条）。
 */

import { create } from 'zustand'

/** 管道累计消耗（track 插件 cumulative.* 透传） */
export interface CumulativeUsage {
  total_input: number
  total_output: number
  total_cached: number
  missed: number
  total_tokens: number
  cache_hit_ratio: number
}

/** 会话级命中率趋势单点（每轮 LLM 调用一条） */
export interface CacheHistoryEntry {
  /** 事件到达时间戳（ms） */
  ts: number
  /** 本轮命中率（0-1） */
  hitRatio: number
  /** 本轮命中 token */
  cachedTokens: number
  /** 本轮未命中 token */
  missedTokens: number
  /** 本轮输入 token */
  inputTokens: number
}

interface PipelineUsage {
  /** prompt token 数（即上下文 token 使用量） */
  promptTokens: number
  /** completion token 数 */
  completionTokens: number
  /** 总 token 数 */
  totalTokens: number
  /** 本轮缓存命中 token（无 cache 数据的事件为 0） */
  cachedTokens: number
  /** 本轮未命中 token（= input - cached） */
  missedTokens: number
  /** 本轮命中率（0-1；无数据为 0） */
  hitRatio: number
  /** 管道累计消耗（无累计数据的事件保持上次的值） */
  cumulative?: CumulativeUsage
  /** 会话级命中率趋势（内存态） */
  cacheHistory: CacheHistoryEntry[]
}

/** cacheHistory 封顶条数（约 200 轮 LLM 调用，防内存膨胀） */
const CACHE_HISTORY_CAP = 200

interface ContextUsageState {
  /** 各管道最新的 usage 数据（key 为 pipelineId） */
  usageByPipeline: Record<string, PipelineUsage>
  /** 更新某个管道的 usage 数据 */
  updateUsage: (pipelineId: string, usage: Record<string, any>) => void
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
      const hasCache = typeof usage.cache_hit_ratio === 'number' || typeof usage.cached_tokens === 'number'
      const historyEntry: CacheHistoryEntry | null = hasCache
        ? {
            ts: Date.now(),
            hitRatio: Number(usage.cache_hit_ratio) || 0,
            cachedTokens: Number(usage.cached_tokens) || 0,
            missedTokens: Number(usage.missed_tokens) || 0,
            inputTokens: Number(usage.input_tokens) || 0,
          }
        : null
      const nextHistory = historyEntry
        ? [...(prev?.cacheHistory ?? []), historyEntry].slice(-CACHE_HISTORY_CAP)
        : prev?.cacheHistory ?? []
      return {
        usageByPipeline: {
          ...state.usageByPipeline,
          [pipelineId]: {
            promptTokens: usage.prompt_tokens || usage.input_tokens || 0,
            completionTokens: usage.completion_tokens || usage.output_tokens || 0,
            totalTokens: usage.total_tokens || 0,
            // 无 cache 字段的事件（如 stream_end.usage）保留上一轮 cache 值，
            // 避免同一轮的两次 usage 更新互相清零
            cachedTokens: hasCache ? Number(usage.cached_tokens) || 0 : prev?.cachedTokens ?? 0,
            missedTokens: hasCache ? Number(usage.missed_tokens) || 0 : prev?.missedTokens ?? 0,
            hitRatio: hasCache ? Number(usage.cache_hit_ratio) || 0 : prev?.hitRatio ?? 0,
            cumulative: usage.cumulative ?? prev?.cumulative,
            cacheHistory: nextHistory,
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
