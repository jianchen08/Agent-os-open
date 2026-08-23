/**
 * 管理员面板 query 集（服务端状态 query 化批次 3）
 *
 * - adminUsers / adminUserStats 两个独立 key：用户列表与统计分开缓存，
 *   互不阻塞（原有 Promise.allSettled 语义——统计失败不阻断列表渲染）
 * - createUser/updateUser/deleteUser 成功后 invalidate 对应 key
 *   （统一 invalidate 两个 key：统计数字随用户变化）
 */

import { useQuery } from '@tanstack/react-query'
import { getUsers, getUserStats } from '@/services/api/users'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'

/** 用户数据新鲜窗口：管理员面板低频页，分钟级陈旧允许 */
const ADMIN_STALE_TIME = 60_000

export function useAdminUsersQuery() {
  return useQuery({
    queryKey: queryKeys.adminUsers,
    // 箭头包裹：getUsers 带 skip/limit 可选参
    queryFn: () => getUsers(),
    staleTime: ADMIN_STALE_TIME,
  })
}

export function useAdminUserStatsQuery() {
  return useQuery({
    queryKey: queryKeys.adminUserStats,
    // 箭头包裹：隔离可选参
    queryFn: () => getUserStats(),
    staleTime: ADMIN_STALE_TIME,
  })
}

/** 用户写操作（create/update/delete）成功后整体失效 */
export function invalidateAdminUsers(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.adminUsers })
  void queryClient.invalidateQueries({ queryKey: queryKeys.adminUserStats })
}
