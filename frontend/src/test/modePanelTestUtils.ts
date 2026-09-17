/**
 * 模式面板测试共享夹具：向共享 query 缓存播种管道 state 摘要。
 * （与 fetchStatesForQuery 同形态：按 pipeline_id 索引）
 */
import type { PipelineStateInfo } from '@/services/api/pipelines'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

/** 播种一条管道 state 摘要进共享缓存 */
export function seedPipelineState(pipelineId: string, state: Record<string, unknown>): void {
  const entry = { pipeline_id: pipelineId, source: 'memory', state } as PipelineStateInfo
  queryClient.setQueryData<Record<string, PipelineStateInfo>>(queryKeys.pipelineStates, (prev) => ({
    ...(prev ?? {}),
    [pipelineId]: entry,
  }))
}
