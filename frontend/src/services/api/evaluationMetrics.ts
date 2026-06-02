/**
 * 评估指标 API 服务
 *
 * 提供评估指标的管理接口
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

/**
 * 评估指标类型
 */
export interface EvaluationMetric {
  /** 指标 ID */
  id: string
  /** 指标名称（唯一） */
  name: string
  /** 指标描述 */
  description: string
  /** 指标分类 */
  category: string
  /** 评估器类型 */
  evaluator_type: string
  /** 评估器标识 */
  evaluator_id: string
  /** 默认配置 */
  default_config?: Record<string, unknown>
  /** 输入参数 Schema */
  input_schema?: Record<string, unknown>
  /** 默认通过阈值 */
  default_pass_threshold?: number
  /** 包含的低级指标 */
  includes?: string[]
  /** 前置依赖 */
  requires?: string[]
  /** 指标层级 */
  level: number
  /** 是否红线指标 */
  is_red_line: boolean
  /** 默认权重 */
  default_weight: number
  /** 来源 */
  source: string
  /** 状态 */
  status: string
  /** 标签 */
  tags?: string[]
  /** 使用次数 */
  usage_count: number
  /** 成功次数 */
  success_count: number
  /** 平均执行时间 */
  avg_execution_time?: number
  /** 创建时间 */
  created_at: string
  /** 更新时间 */
  updated_at?: string
}

/**
 * 评估指标列表响应
 */
export interface EvaluationMetricsListResponse {
  /** 指标列表 */
  metrics: EvaluationMetric[]
  /** 总数量 */
  total: number
}

/**
 * 获取评估指标列表
 *
 * @param params 查询参数
 * @returns 评估指标列表
 */
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

/**
 * 获取单个评估指标
 *
 * @param id 指标 ID
 * @returns 评估指标详情
 */
export async function getEvaluationMetric(id: string): Promise<EvaluationMetric> {
  const response = await apiClient.get<EvaluationMetric>(API_ENDPOINTS.EVALUATION.METRIC(id))
  return response.data
}

/**
 * 删除评估指标
 *
 * @param id 指标 ID
 * @returns 是否成功
 */
export async function deleteEvaluationMetric(id: string): Promise<boolean> {
  try {
    await apiClient.delete(API_ENDPOINTS.EVALUATION.METRIC(id))
    return true
  } catch {
    return false
  }
}
