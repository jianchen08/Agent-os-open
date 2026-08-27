/**
 * 全局搜索 API 服务（统一搜索）
 *
 * 调用插件端点 GET monitoring 插件 search 统一搜索会话与消息（生成物投影）。
 * 会话结果来自 MemoryStore 标题匹配，消息结果来自 ExecutionRecordStorage 内容匹配。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

export type SearchType = 'all' | 'session' | 'message'

export interface SessionSearchHit {
  id: string
  title: string
  updated_at: string
  message_count: number
}

export interface MessageSearchHit {
  id: string
  /** 所属会话/管道 ID */
  session_id: string
  role: string
  /** 消息内容（截断到 200 字符） */
  content: string
  timestamp: string
  sequence: number
}

export interface SearchResponse {
  query: string
  type: SearchType
  sessions: SessionSearchHit[]
  messages: MessageSearchHit[]
}

/**
 * 全局搜索（会话 + 消息）
 *
 * @param q 搜索关键词
 * @param type 搜索类型：all/session/message
 * @param limit 每类结果数量上限
 * @param options 重试选项
 */
export async function searchGlobal(
  q: string,
  type: SearchType = 'all',
  limit = 20,
  options: RetryOptions = {},
): Promise<SearchResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<SearchResponse>(API_ENDPOINTS.SEARCH.GLOBAL, {
      params: { q, type, limit },
    })
    return response.data
  }, options)
}
