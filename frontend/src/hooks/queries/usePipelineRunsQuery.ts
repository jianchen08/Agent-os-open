/**
 * 管道运行注册表 query（服务端状态 query 化）
 *
 * runs/states 数据唯一真值源 = TanStack Query 缓存：
 * - queryKeys.pipelineRuns（GET /api/v1/pipelines/runs，同管道多条 run 去重取最新）
 * - queryKeys.pipelineStates（GET /api/v1/pipelines/state，pipeline_id → 摘要）
 *
 * 轮询语义（替代原 pipelineRegistryStore.startAutoRefresh 的 30s setInterval）：
 * - refetchInterval: 30_000 —— 兜底对账频率与旧实现一致
 * - refetchOnWindowFocus 全局关闭：焦点/可见性事件噪声大
 *   （最大化、切全屏均触发），新鲜度由 30s 轮询 + WS 事件 invalidate 承担
 * - 页面隐藏自动暂停轮询（refetchIntervalInBackground 默认 false）
 *
 * 组件消费走 usePipelineRunsQuery / usePipelineStatesQuery；非组件代码
 * （WS 事件 handler / store 编排 action）读写走 readXxx / updateXxxCache /
 * invalidateXxx——用全局 queryClient 单例，不 new QueryClient。
 *
 * runs 与 states 各自独立 query：states 拉取失败不影响 runs（天然降级，
 * 等价旧 fetch 的 statesError 留痕语义，错误信息由 statesQuery.error 暴露）。
 */

import { useQuery } from '@tanstack/react-query'
import {
  fetchPipelineRuns,
  fetchPipelineStates,
  type PipelineStateInfo,
} from '@/services/api/pipelines'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import type { PipelineRunInfo } from '@/types/pipeline'

/** 管道快照兜底轮询间隔（ms）：与旧 pipelineRegistryStore 一致 */
const PIPELINE_REGISTRY_REFRESH_INTERVAL = 30_000

/** 运行条目 key：pipeline_id 优先，缺失回退 run_id（原 registry 同款） */
function entryKey(item: PipelineRunInfo): string {
  return item.pipeline_id || item.run_id
}

/** runs 数组 → 注册表 Record：同管道多条 run 取开始时间最新的一条 */
export function mapRunsToRecord(items: PipelineRunInfo[]): Record<string, PipelineRunInfo> {
  const next: Record<string, PipelineRunInfo> = {}
  for (const item of items) {
    const key = entryKey(item)
    const existing = next[key]
    if (!existing || item.started_at >= existing.started_at) {
      next[key] = item
    }
  }
  return next
}

/** states 数组 → pipeline_id 索引 Record */
function mapStatesToRecord(items: PipelineStateInfo[]): Record<string, PipelineStateInfo> {
  const next: Record<string, PipelineStateInfo> = {}
  for (const st of items) {
    next[st.pipeline_id] = st
  }
  return next
}

/** queryFn：拉取 runs 快照并归并（原 registry fetch 的 runs 侧逻辑） */
async function fetchRunsForQuery(): Promise<Record<string, PipelineRunInfo>> {
  const items = await fetchPipelineRuns({ limit: 100 })
  return mapRunsToRecord(items)
}

/** queryFn：拉取 states 摘要（失败由 query error 态承载，不影响 runs） */
async function fetchStatesForQuery(): Promise<Record<string, PipelineStateInfo>> {
  const items = await fetchPipelineStates()
  return mapStatesToRecord(items)
}

/** 管道 runs 快照 query：挂载即拉取 + 30s 兜底轮询 */
export function usePipelineRunsQuery() {
  return useQuery({
    queryKey: queryKeys.pipelineRuns,
    queryFn: fetchRunsForQuery,
    refetchInterval: PIPELINE_REGISTRY_REFRESH_INTERVAL,
  })
}

/** 管道 states 摘要 query：挂载即拉取 + 30s 兜底轮询 */
export function usePipelineStatesQuery() {
  return useQuery({
    queryKey: queryKeys.pipelineStates,
    queryFn: fetchStatesForQuery,
    refetchInterval: PIPELINE_REGISTRY_REFRESH_INTERVAL,
  })
}

/** 非组件环境读当前缓存的 runs 注册表（无缓存返回空对象，调用方无需空值分支） */
export function readPipelineRuns(): Record<string, PipelineRunInfo> {
  return queryClient.getQueryData<Record<string, PipelineRunInfo>>(queryKeys.pipelineRuns) ?? {}
}

/** 更新缓存的 runs 注册表（WS 流式事件增量写；无缓存视同 {}） */
export function updatePipelineRunsCache(
  updater: (prev: Record<string, PipelineRunInfo>) => Record<string, PipelineRunInfo>,
): void {
  queryClient.setQueryData<Record<string, PipelineRunInfo>>(
    queryKeys.pipelineRuns,
    (prev) => updater(prev ?? {}),
  )
}

/** 标记 runs 缓存失效（WS 重连后对账用；活跃订阅自动重拉） */
export function invalidatePipelineRuns(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.pipelineRuns })
}

/** 标记 states 缓存失效（WS 重连后对账用；活跃订阅自动重拉） */
export function invalidatePipelineStates(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.pipelineStates })
}
