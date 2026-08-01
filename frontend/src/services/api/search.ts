/**
 * 全局搜索 API 服务（P2 搜索框合并）
 *
 * 调用后端 GET /api/v1/search 统一搜索会话与消息。
 * 会话结果来自 MemoryStore 标题匹配，消息结果来自 ExecutionRecordStorage 内容匹配。
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/** 搜索类型 */
export type SearchType = 'all' | 'session' | 'message'

/** 会话搜索结果项 */
export interface SessionSearchHit {
  /** 会话 ID */
  id: string
  /** 会话标题 */
  title: string
  /** 更新时间 */
  updated_at: string
  /** 消息数量 */
  message_count: number
}

/** 消息搜索结果项 */
export interface MessageSearchHit {
  /** 消息记录 ID */
  id: string
  /** 所属会话/管道 ID */
  session_id: string
  /** 消息角色 */
  role: string
  /** 消息内容（截断到 200 字符） */
  content: string
  /** 时间戳 */
  timestamp: string
  /** 序号 */
  sequence: number
}

/** 搜索响应 */
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
