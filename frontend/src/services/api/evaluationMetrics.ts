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
  // 后端评估服务保证 metric 字段全量返回（category/usage_count/success_count/
  // created_at 恒存在，见 plugins/shared/system/evaluation/server.py 的列表组装），
  // 直接透传；不再以 ''/0 伪造补齐缺失字段（伪造会让「无统计」与「未上报」不可区分）。
  return {
    metrics: response.data.metrics,
    total: response.data.total,
  }
}
