/**
 * 恢复可见即对账重拉（离屏暂停的"可见即恢复"半边）
 *
 * 离屏期间轮询被冻结，数据停留在暂停时刻；重新可见时立即 invalidate 一次，
 * 不等下一个轮询周期。仅处理 false→true 跳变：挂载（初值即 true）不触发，
 * 避免与查询自身的挂载拉取重复请求。
 */
import { useEffect, useRef } from 'react'
import type { QueryClient, QueryKey } from '@tanstack/react-query'

export function useVisibleRefetch(
  visible: boolean,
  keys: QueryKey[],
  queryClient: QueryClient,
): void {
  const wasVisible = useRef(true)
  useEffect(() => {
    const resumed = visible && !wasVisible.current
    wasVisible.current = visible
    if (!resumed) return
    for (const key of keys) {
      void queryClient.invalidateQueries({ queryKey: key })
    }
  }, [visible, keys, queryClient])
}
