/**
 * 记忆管理 API 服务
 *
 * 提供情景记忆和语义记忆的管理接口，与后端 /api/v1/memory/* 端点对齐
 *
 * 暴露接口：
 * - getEpisodes(page, pageSize, options): EpisodesListResponse - 获取情景记忆列表
 * - getEpisode(id, options): Episode - 获取单个情景记忆
 * - searchMemory(query, options): MemorySearchResponse - 搜索记忆
 * - getSemanticMemory(options): 语义记忆列表 - 获取语义记忆列表
 * - consolidateMemory(options): 整合结果 - 记忆整合
 * - getMemoryStats(options): MemoryStats - 获取记忆统计
 * - MemoryItem - 记忆项类型
 * - MemorySearchResponse - 记忆搜索响应
 * - MemorySearchRequest - 记忆搜索请求
 * - Episode - 情景记忆类型
 * - EpisodesListResponse - 情景记忆列表响应
 * - SemanticKnowledge - 语义知识类型
 * - MemoryStats - 记忆统计类型
 */

import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { requestWithRetry } from '@/utils/retry'
import type { RetryOptions } from '@/utils/retry'

/**
 * 记忆项类型
 */
export interface MemoryItem {
  /** 记忆 ID */
  id: string
  /** 内容 */
  content: string
  /** 记忆类型 */
  memory_type: string
  /** 相关性得分 */
  score: number
  /** 元数据 */
  metadata?: Record<string, unknown>
  /** 创建时间 */
  created_at: string
}

/**
 * 记忆搜索响应
 */
export interface MemorySearchResponse {
  /** 搜索结果 */
  items: MemoryItem[]
  /** 总数量 */
  total: number
  /** 搜索查询 */
  query: string
}

/**
 * 记忆搜索请求
 */
export interface MemorySearchRequest {
  /** 搜索查询 */
  query: string
  /** 记忆类型过滤 */
  memory_types?: string[]
  /** 返回数量 */
  top_k?: number
  /** 最小相关性得分 */
  min_score?: number
}

/**
 * 情景记忆类型
 */
export interface Episode {
  /** 记忆 ID */
  id: string
  /** 意图文本 */
  intent_text: string
  /** 执行计划 */
  plan_dag?: Record<string, unknown>
  /** 执行摘要 */
  execution_summary?: string
  /** 评估报告 */
  evaluation_report?: Record<string, unknown>
  /** 最终得分 */
  final_score?: number
  /** 标签 */
  tags: string[]
  /** 创建时间 */
  created_at: string
}

/**
 * 情景记忆列表响应
 */
export interface EpisodesListResponse {
  /** 情景记忆列表 */
  items: Episode[]
  /** 总数量 */
  total: number
  /** 当前页码 */
  page: number
  /** 每页数量 */
  page_size: number
}

/**
 * 语义知识类型
 */
export interface SemanticKnowledge {
  /** 知识 ID */
  id: string
  /** 内容 */
  content: string
  /** 来源类型 */
  source_type: string
  /** 额外数据 */
  extra_data?: Record<string, unknown>
  /** 创建时间 */
  created_at: string
}

/**
 * 记忆统计类型
 */
export interface MemoryStats {
  /** 情景记忆数量 */
  episode_count: number
  /** 语义知识数量 */
  knowledge_count: number
  /** 总记忆数 */
  total_count: number
  /** 最近更新时间 */
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

export async function getEpisode(id: string, options: RetryOptions = {}): Promise<Episode> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<Episode>(API_ENDPOINTS.MEMORY.EPISODE(id))
    return response.data
  }, options)
}

export async function searchMemory(
  query: string | MemorySearchRequest,
  options: RetryOptions = {},
): Promise<MemorySearchResponse> {
  return requestWithRetry(async () => {
    const requestData = typeof query === 'string' ? { query, top_k: 10, min_score: 0.5 } : query

    const response = await apiClient.post<MemorySearchResponse>(
      API_ENDPOINTS.MEMORY.SEARCH,
      requestData,
    )
    return response.data
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

export async function consolidateMemory(
  options: RetryOptions = {},
): Promise<{ success: boolean; message: string; consolidated_count?: number }> {
  return requestWithRetry(async () => {
    const response = await apiClient.post<{
      success: boolean
      message: string
      consolidated_count?: number
    }>(API_ENDPOINTS.MEMORY.CONSOLIDATE)
    return response.data
  }, options)
}

export async function getMemoryStats(options: RetryOptions = {}): Promise<MemoryStats> {
  return requestWithRetry(async () => {
    const response = await apiClient.get<MemoryStats>(API_ENDPOINTS.MEMORY.STATS)
    return response.data
  }, options)
}
