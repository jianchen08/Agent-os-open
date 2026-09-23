/**
 * 管道诊断 API 服务：step 级 trace 时间线 + pipeline_state 全字段
 *
 * 数据面 = monitoring 插件 /ext/monitoring/traces 与 /ext/monitoring/pipeline-state
 * （内核只读能力桥：traces.list_by_pipeline / pipeline-runs.list_by_pipeline /
 * pipeline-state.list / db-admin.table_query）。
 */

import apiClient from '@/services/api/client'
import { MONITORING_ENDPOINTS } from './endpoints.generated'

/** trace 行 llm_usage（llm_core patch 携带，按 model 归属；数字字段可缺——
 *  空 LLM 轮/部分 usage 路径只带归属键，消费面须判空） */
export interface TraceLlmUsage {
  input_tokens?: number
  output_tokens?: number
  total_tokens?: number
  cached_tokens?: number
  model?: string
  provider?: string
}

/** 单条 step 级 trace（patch_data 为状态窗口原文，前端展开查看；traces 为 pipeline 级 op 流） */
export interface PipelineTraceRow {
  trace_id: string
  pipeline_id: string
  seq: number | null
  plugin_id: string
  patch_type: string
  created_at: string
  iteration: number | null
  summary: string | null
  llm_usage: TraceLlmUsage | null
  error: string | null
  tool_call_count: number
  patch_data: Record<string, unknown>
}

export interface PipelineTracesResponse {
  traces: PipelineTraceRow[]
  total: number
  pipeline_id: string
}

/** pipeline_state 单字段行（value 为 DB 标量原文，展示时尝试 JSON pretty；标量化见 ADR 2026-09-18） */
export interface PipelineStateField {
  field_key: string
  field_value: string | null
  value_kind?: string
  updated_at: string | null
}

export interface PipelineRunRow {
  run_id: string
  status?: string
  started_at?: string
  ended_at?: string
  [key: string]: unknown
}

export interface PipelineStateFullResponse {
  pipeline_id: string
  fields: PipelineStateField[]
  runs: PipelineRunRow[]
  summary: Record<string, unknown> | null
}

/** 单管道 step 级 trace 时间线（seq 升序，limit 尾部截取最近条目） */
export async function getPipelineTraces(params: {
  pipeline_id: string
  limit?: number
}): Promise<PipelineTracesResponse> {
  const response = await apiClient.get<PipelineTracesResponse>(
    MONITORING_ENDPOINTS.mon_traces_by_pipeline,
    { params },
  )
  return response.data
}

/** 单管道 state 全字段（不经 export_fields 白名单裁剪）+ runs + 摘要行 */
export async function getPipelineStateFull(
  pipelineId: string,
): Promise<PipelineStateFullResponse> {
  const response = await apiClient.get<PipelineStateFullResponse>(
    MONITORING_ENDPOINTS.mon_pipeline_state_full,
    { params: { pipeline_id: pipelineId } },
  )
  return response.data
}
