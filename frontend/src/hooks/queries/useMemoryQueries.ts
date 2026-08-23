/**
 * 记忆管理页 query 集（服务端状态 query 化批次 3）
 *
 * - memoryStats：静态 key，staleTime 60s 窗口内重挂零请求
 * - memoryEpisodes(page)：页码进 key，翻页 = 换缓存条目（带缓存立即渲染，
 *   新页无缓存时旧数据占位 + 后台重拉，不闪 loading）
 * - 语义记忆仍走 tab 切换时的显式拉取（沿用原有「切 tab 才拉」语义，
 *   以 query 缓存命中判断是否重拉——length===0 语义保持）
 */

import { useQuery } from '@tanstack/react-query'
import { getEpisodes, getMemoryStats } from '@/services/api/memory'
import { queryKeys } from '@/services/query/queryKeys'

/** 记忆数据新鲜窗口：分钟级陈旧允许 */
const MEMORY_STALE_TIME = 60_000

/** 情景记忆分页条数（页面固定 10） */
export const MEMORY_EPISODES_PAGE_SIZE = 10

export function useMemoryStatsQuery() {
  return useQuery({
    queryKey: queryKeys.memoryStats,
    // 箭头包裹：隔离 getMemoryStats 的 RetryOptions 可选参
    queryFn: () => getMemoryStats(),
    staleTime: MEMORY_STALE_TIME,
  })
}

export function useMemoryEpisodesQuery(page: number) {
  return useQuery({
    queryKey: queryKeys.memoryEpisodes(page),
    // 箭头包裹：隔离 getEpisodes 的 RetryOptions 可选参
    queryFn: () => getEpisodes(page, MEMORY_EPISODES_PAGE_SIZE),
    staleTime: MEMORY_STALE_TIME,
    // 翻页时保留上一页缓存数据占位（页间切换不闪 loading）
    placeholderData: (previousData) => previousData,
  })
}
