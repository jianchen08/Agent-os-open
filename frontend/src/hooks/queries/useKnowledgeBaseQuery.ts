/**
 * 知识库页 query（服务端状态 query 化批次 3）
 *
 * 原 4 个并发请求（列表/统计/分类/标签）合并为一个 queryFn：
 * - 同一 staleTime 窗口内重挂零请求（4 个请求同生共死，数据一起新鲜/一起失效）
 * - 各分项部分失败不互相阻塞（Promise.allSettled 语义保留），失败项降级默认值
 * - upload/delete/create/deleteCategory 成功后 invalidateKnowledgeBaseCache
 *   整体失效（statistics 随文件变化，四者一体刷新）
 *
 * 数据结构：{ items, stats, categories, tags } 四元组一次返回。
 */

import { useQuery } from '@tanstack/react-query'
import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

/** 知识库条目 */
export interface KnowledgeItem {
  id: string
  name: string
  size: number
  categories: string[]
  tags: string[]
  created_at?: string
  updated_at?: string
  [key: string]: unknown
}

/** 知识库统计 */
export interface KnowledgeStats {
  total: number
  categories_count: number
  tags_count: number
  [key: string]: unknown
}

/** 分类信息 */
export interface CategoryItem {
  name: string
  count?: number
  [key: string]: unknown
}

/** 知识库整体数据（四部分一次取回） */
export interface KnowledgeBaseData {
  items: KnowledgeItem[]
  stats: KnowledgeStats | null
  categories: CategoryItem[]
  tags: string[]
}

/** 数据新鲜窗口：知识库变化频率低（上传/删除才变，事件驱动失效），放宽 60s */
const KB_STALE_TIME = 60_000

export function useKnowledgeBaseQuery() {
  return useQuery({
    queryKey: queryKeys.kbFiles,
    queryFn: async (): Promise<KnowledgeBaseData> => {
      const [itemsRes, statsRes, catRes, tagsRes] = await Promise.allSettled([
        apiClient.get<KnowledgeItem[]>(API_ENDPOINTS.KNOWLEDGE_BASE.LIST),
        apiClient.get<KnowledgeStats>(API_ENDPOINTS.KNOWLEDGE_BASE.STATS),
        apiClient.get<CategoryItem[]>(API_ENDPOINTS.KNOWLEDGE_BASE.CATEGORIES),
        apiClient.get<string[]>(API_ENDPOINTS.KNOWLEDGE_BASE.TAGS),
      ])
      return {
        items: itemsRes.status === 'fulfilled' && Array.isArray(itemsRes.value.data) ? itemsRes.value.data : [],
        stats: statsRes.status === 'fulfilled' ? statsRes.value.data : null,
        categories:
          catRes.status === 'fulfilled' && Array.isArray(catRes.value.data) ? catRes.value.data : [],
        tags: tagsRes.status === 'fulfilled' && Array.isArray(tagsRes.value.data) ? tagsRes.value.data : [],
      }
    },
    staleTime: KB_STALE_TIME,
  })
}

/** 上传/删除/分类变更后整体失效（写操作后调用，后台重拉新数据） */
export function invalidateKnowledgeBaseCache(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.kbFiles })
}
