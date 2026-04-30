/**
 * 执行图 API 服务
 *
 * 提供 getGraph 接口，通过线程详情端点获取执行图数据
 *
 * Requirements: 1.4, 5.1
 *
 * 暴露接口：
 * - getGraph(sessionId, options): GraphData - 获取会话执行图
 * - getThreadDetail(threadId, options): ThreadDetailResponse - 获取线程详情
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { mapThreadDetailToGraph, type ThreadDetailResponse } from '@/utils/mappers'
import { requestWithRetry } from '@/utils/retry'
import type { GraphData } from '@/types/graph'
import type { RetryOptions } from '@/utils/retry'

/**
 * 参数验证错误
 */
class ValidationError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ValidationError'
  }
}

/**
 * 验证会话ID
 */
function validateSessionId(sessionId: string): void {
  if (!sessionId || sessionId.trim().length === 0) {
    throw new ValidationError('会话ID不能为空')
  }
}

export async function getGraph(sessionId: string, options: RetryOptions = {}): Promise<GraphData> {
  // 参数验证
  validateSessionId(sessionId)

  return requestWithRetry(async () => {
    // 调用线程详情端点获取完整线程信息（包含执行图）
    const response = await apiClient.get<ThreadDetailResponse>(API_ENDPOINTS.THREADS.GET(sessionId))

    // 使用数据映射函数将线程详情中的执行图转换为前端GraphData模型
    return mapThreadDetailToGraph(response.data)
  }, options)
}

export async function getThreadDetail(
  threadId: string,
  options: RetryOptions = {},
): Promise<ThreadDetailResponse> {
  // 参数验证
  validateSessionId(threadId)

  return requestWithRetry(async () => {
    const response = await apiClient.get<ThreadDetailResponse>(API_ENDPOINTS.THREADS.GET(threadId))
    return response.data
  }, options)
}
