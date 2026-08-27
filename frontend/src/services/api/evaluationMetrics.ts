/**
 * 评估指标 API 服务
 *
 * 提供评估指标的管理接口
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

export interface EvaluationMetric {
  id: string
  name: string
  description: string
  category: string
  evaluator_type: string
  evaluator_id: string
  default_config?: Record<string, unknown>
  input_schema?: Record<string, unknown>
  default_pass_threshold?: number
  /** 复合指标包含的子指标 ID 列表 */
  includes?: string[]
  /** 前置依赖的指标 ID 列表 */
  requires?: string[]
  level: number
  is_red_line: boolean
  default_weight: number
  source: string
  status: string
  tags?: string[]
  usage_count: number
  success_count: number
  avg_execution_time?: number
  created_at: string
  updated_at?: string
}

export interface EvaluationMetricsListResponse {
  metrics: EvaluationMetric[]
  total: number
}

export async function getEvaluationMetrics(params?: {
  skip?: number
  limit?: number
  category?: string
  status?: string
  metric_type?: string
}): Promise<{ metrics: EvaluationMetric[]; total: number }> {
  const response = await apiClient.get<EvaluationMetricsListResponse>(
    API_ENDPOINTS.EVALUATION.METRICS,
    { params },
  )
  return {
    metrics: response.data.metrics.map((item) => ({
      ...item,
      category: (item as any).category || (item as any).metric_type || '',
      usage_count: (item as any).usage_count ?? 0,
      success_count: (item as any).success_count ?? 0,
      created_at: (item as any).created_at ?? '',
    })),
    total: response.data.total,
  }
}
