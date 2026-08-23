/**
 * 管道运行注册表 Store（统一管道管理）
 *
 * 服务端状态 query 化批次 4：runs/states 数据容器已换 TanStack Query 缓存
 * （hooks/queries/usePipelineRunsQuery，queryKeys.pipelineRuns/pipelineStates）——
 * 快照拉取/30s 兜底轮询/窗口聚焦刷新由 usePipelineRunsQuery/usePipelineStatesQuery
 * 承担。本 store 保留 applyStreamStatus 编排层（签名不变，streaming handler
 * 调用点零改动），内部改写 query cache；fetch/startAutoRefresh/stopAutoRefresh
 * 已退役（消费方改挂 query hook）。
 *
 * 数据源分层（沿用原语义）：
 * - 快照：`GET /api/v1/pipelines/runs`（query 化，初始化/窗口聚焦/兜底轮询/重连 invalidate）
 * - 实时：`stream_start`/`stream_end`/`stream_error` 事件增量更新（会话管道，
 *   applyStreamStatus → updatePipelineRunsCache）
 * - 任务管道状态以长期任务 query（useLongTermTasksQuery，5s 轮询）为准，
 *   面板侧派生条目，不在此注册表维护（任务 WS 事件在 0.2 侧为静默跳过状态）。
 */

import { create } from 'zustand'
import { usePipelineMessageStore } from './pipelineMessageStore'
import {
  readPipelineRuns,
  updatePipelineRunsCache,
} from '@/hooks/queries/usePipelineRunsQuery'
import type { PipelineRunInfo, PipelineStatus } from '@/types/pipeline'

interface PipelineRegistryState {
  /** 流式事件同步：stream_start→running / stream_end→completed / stream_error→failed
   *  （增量写 runs query cache，保持 streaming handler 调用点签名不变） */
  applyStreamStatus: (pipelineId: string, status: PipelineStatus) => void
  /** 重置（登出等场景）：清空本地编排态 + runs 缓存 */
  reset: () => void
}

export const usePipelineRegistryStore = create<PipelineRegistryState>()(() => ({
  applyStreamStatus: (pipelineId, status) => {
    if (!pipelineId) return
    const now = new Date().toISOString()
    updatePipelineRunsCache((runs) => {
      const existing = runs[pipelineId]
      if (existing) {
        const updated: PipelineRunInfo = { ...existing, status }
        // 结束事件补 ended_at（运行中的 ended_at 保持原值）
        if (status === 'completed' || status === 'failed') {
          if (!updated.ended_at) updated.ended_at = now
        } else {
          updated.ended_at = undefined
        }
        return { ...runs, [pipelineId]: updated }
      }
      // 新管道：从 pipelineMessageStore 反查归属会话
      const pipeStore = usePipelineMessageStore.getState()
      const threadId =
        pipeStore.pipelineSessionMap[pipelineId]
        || pipeStore.pipelines[pipelineId]?.sessionId
      return {
        ...runs,
        [pipelineId]: {
          run_id: pipelineId,
          pipeline_id: pipelineId,
          thread_id: threadId,
          status,
          started_at: now,
        },
      }
    })
  },

  reset: () => {
    // 清空 runs query 缓存（states 随 queryClient.clear 或各自失效处理）
    if (Object.keys(readPipelineRuns()).length > 0) {
      updatePipelineRunsCache(() => ({}))
    }
  },
}))
