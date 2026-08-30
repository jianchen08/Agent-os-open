/**
 * 记忆管理 API 服务
 *
 * 提供情景记忆和语义记忆的管理接口，与后端 /api/v1/memory/* 端点对齐
 */

import { API_ENDPOINTS } from '@/constants/api'
import { HINDSIGHT_MEMORY_SERVICE_ENDPOINTS } from './endpoints.generated'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

export interface MemoryItem {
  id: string
  content: string
  memory_type: string
  score: number
  metadata?: Record<string, unknown>
  created_at: string
}

export interface MemorySearchResponse {
  items: MemoryItem[]
  total: number
  query: string
}

export interface Episode {
  id: string
  intent_text: string
  plan_dag?: Record<string, unknown>
  execution_summary?: string
  evaluation_report?: Record<string, unknown>
  final_score?: number
  tags: string[]
  created_at: string
}

export interface EpisodesListResponse {
  items: Episode[]
  total: number
  page: number
  page_size: number
}

export interface SemanticKnowledge {
  id: string
  content: string
  source_type: string
  extra_data?: Record<string, unknown>
  created_at: string
}

export interface MemoryStats {
  episode_count: number
  knowledge_count: number
  total_count: number
  last_updated: string
}

export async function getEpisodes(
  page: number = 1,
  pageSize: number = 20,
  options: RetryOptions = {},
): Promise<EpisodesListResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<EpisodesListResponse>(API_ENDPOINTS.MEMORY.EPISODES, {
      params: { page, page_size: pageSize },
    })
    return response.data
  }, options)
}

/**
 * Hindsight 语义检索（成熟包数据面，widget 化 B3 收口）：
 * GET hindsight_memory_service recall → {results, total} → {items,total,query}。
 * 记忆数据已接入成熟 Hindsight（向量检索，嵌入式 PostgreSQL），本函数是
 * 前端消费入口（替代已退役 channel_api memory 域 search）。
 */
export async function searchHindsight(
  query: string,
  top_k = 10,
  options: RetryOptions = {},
): Promise<MemorySearchResponse> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{ results: unknown[]; total: number }>(
      `${HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.hindsight_recall}?query=${encodeURIComponent(query)}&limit=${top_k}`,
    )
    const results = response.data.results ?? []
    return {
      items: results
        .filter((r) => r && typeof r === 'object')
        .map((r) => {
          const item = r as Record<string, unknown>
          const id = String(item.id ?? '')
          const content =
            typeof item.content === 'string'
              ? item.content
              : typeof item.text === 'string'
                ? item.text
                : ''
          const meta = (item.metadata as Record<string, unknown> | undefined) ?? {}
          return {
            id,
            content,
            memory_type: String(meta.memory_type ?? 'semantic'),
            score: typeof item.score === 'number' ? item.score : 0,
            metadata: meta,
            created_at: '',
          } as MemoryItem
        }),
      total: response.data.total ?? results.length,
      query,
    }
  }, options)
}

export async function getSemanticMemory(
  options: RetryOptions = {},
): Promise<{ items: SemanticKnowledge[]; total: number }> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<{
      items: SemanticKnowledge[]
      total: number
    }>(API_ENDPOINTS.MEMORY.SEMANTIC)
    return response.data
  }, options)
}

// 本服务不提供 importDocument：后端无 /api/v1/memory/import 端点。

export async function getMemoryStats(options: RetryOptions = {}): Promise<MemoryStats> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<MemoryStats>(API_ENDPOINTS.MEMORY.STATS)
    return response.data
  }, options)
}
