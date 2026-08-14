/**
 * 管道运行注册表 Store（统一管道管理）
 *
 * 数据源分层：
 * - 快照：`GET /api/v1/pipelines/runs`（内核四表联结，初始化/重连/兜底轮询）
 * - 实时：`stream_start`/`stream_end`/`stream_error` 事件增量更新（会话管道）
 * - 任务管道状态以 `longTermTaskStore`（5s 轮询）为准，面板侧派生条目，
 *   不在此 store 维护（任务 WS 事件在 0.2 侧为静默跳过状态，见 tool.py 注释）。
 */

import { create } from 'zustand'
import { fetchPipelineRuns } from '@/services/api/pipelines'
import type { PipelineRunInfo, PipelineStatus } from '@/types/pipeline'
import { usePipelineMessageStore } from './pipelineMessageStore'

/** 兜底轮询间隔：事件驱动为主，轮询仅兜底对账 */
const REFRESH_INTERVAL_MS = 30_000

/** 运行条目 key：pipeline_id 优先，缺失回退 run_id */
function entryKey(item: PipelineRunInfo): string {
  return item.pipeline_id || item.run_id
}

interface PipelineRegistryState {
  /** 运行快照（同管道多条 run 取最新一条） */
  runs: Record<string, PipelineRunInfo>
  /** 最近一次快照拉取时间 */
  lastFetchedAt: number | null
  /** 是否正在拉取 */
  isLoading: boolean
  /** 错误信息（拉取失败时置位，可静默重试） */
  error: string | null
  _refreshTimer: ReturnType<typeof setInterval> | null
  _visibilityHandler: (() => void) | null

  /** 拉取管道运行快照（合并进注册表，不覆盖本地增量状态） */
  fetch: () => Promise<void>
  /** 流式事件同步：stream_start→running / stream_end→completed / stream_error→failed */
  applyStreamStatus: (pipelineId: string, status: PipelineStatus) => void
  /** 启动自动刷新（30s 轮询 + 页面可见时拉取）；由面板挂载时调用 */
  startAutoRefresh: () => void
  /** 停止自动刷新（面板卸载时调用） */
  stopAutoRefresh: () => void
  /** 重置（登出等场景） */
  reset: () => void
}

export const usePipelineRegistryStore = create<PipelineRegistryState>((set, get) => ({
  runs: {},
  lastFetchedAt: null,
  isLoading: false,
  error: null,
  _refreshTimer: null,
  _visibilityHandler: null,

  fetch: async () => {
    if (get().isLoading) return
    set({ isLoading: true })
    try {
      const items = await fetchPipelineRuns({ limit: 100 })
      const next: Record<string, PipelineRunInfo> = {}
      for (const item of items) {
        const key = entryKey(item)
        const existing = next[key]
        // 同管道多条 run 取开始时间最新的一条
        if (!existing || item.started_at >= existing.started_at) {
          next[key] = item
        }
      }
      set({ runs: next, lastFetchedAt: Date.now(), error: null, isLoading: false })
    } catch (e) {
      console.error('[pipelineRegistry] 拉取管道快照失败', e)
      set({ error: e instanceof Error ? e.message : '拉取管道快照失败', isLoading: false })
    }
  },

  applyStreamStatus: (pipelineId, status) => {
    if (!pipelineId) return
    const now = new Date().toISOString()
    set((state) => {
      const existing = state.runs[pipelineId]
      if (existing) {
        const updated: PipelineRunInfo = { ...existing, status }
        // 结束事件补 ended_at（运行中的 ended_at 保持原值）
        if (status === 'completed' || status === 'failed') {
          if (!updated.ended_at) updated.ended_at = now
        } else {
          updated.ended_at = undefined
        }
        return { runs: { ...state.runs, [pipelineId]: updated } }
      }
      // 新管道：从 pipelineMessageStore 反查归属会话
      const pipeStore = usePipelineMessageStore.getState()
      const threadId =
        pipeStore.pipelineSessionMap[pipelineId]
        || pipeStore.pipelines[pipelineId]?.sessionId
      return {
        runs: {
          ...state.runs,
          [pipelineId]: {
            run_id: pipelineId,
            pipeline_id: pipelineId,
            thread_id: threadId,
            status,
            started_at: now,
          },
        },
      }
    })
  },

  startAutoRefresh: () => {
    const state = get()
    if (state._refreshTimer) return
    const timer = setInterval(() => {
      if (document.visibilityState === 'visible') {
        get().fetch()
      }
    }, REFRESH_INTERVAL_MS)
    const handler = () => {
      if (document.visibilityState === 'visible') {
        get().fetch()
      }
    }
    document.addEventListener('visibilitychange', handler)
    set({ _refreshTimer: timer, _visibilityHandler: handler })
  },

  stopAutoRefresh: () => {
    const state = get()
    if (state._refreshTimer) {
      clearInterval(state._refreshTimer)
    }
    if (state._visibilityHandler) {
      document.removeEventListener('visibilitychange', state._visibilityHandler)
    }
    set({ _refreshTimer: null, _visibilityHandler: null })
  },

  reset: () => {
    get().stopAutoRefresh()
    set({ runs: {}, lastFetchedAt: null, isLoading: false, error: null })
  },
}))
