/**
 * 会话列表 query（服务端状态 query 化）
 *
 * sessions 数据唯一真值源 = TanStack Query 缓存（queryKeys.sessions）：
 * - 组件消费走 useSessionsQuery（缓存秒开 + staleTime 到期后台静默刷新）
 * - 非组件代码读写走 readSessions/updateSessionsCache（sessionListStore 的
 *   乐观更新、跨 store 副作用等编排逻辑仍住在 zustand action 里，只是数据
 *   容器换成 query cache，避免 store 镜像 + cache 的双真值源）
 */

import { useQuery } from '@tanstack/react-query'
import { getSessions } from '@/services/api/session'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import type { Session } from '@/types/models'

/** 会话列表数据新鲜窗口：窗口内切页/重挂零请求，到期后台静默刷新 */
const SESSIONS_STALE_TIME = 30_000

export function useSessionsQuery() {
  return useQuery({
    queryKey: queryKeys.sessions,
    // 箭头包裹：隔离 getSessions 的 RetryOptions 可选参，避免 QueryFunctionContext 误传入
    queryFn: () => getSessions(),
    staleTime: SESSIONS_STALE_TIME,
  })
}

/** 确保会话列表已加载（缓存命中零请求；无缓存时拉取一次并写入缓存）。
 *  供非组件流程在依赖会话归属判定前显式等待（如管道跳转防误判孤儿）。 */
export async function ensureSessionsLoaded(): Promise<Session[]> {
  const cached = queryClient.getQueryData<Session[]>(queryKeys.sessions)
  if (cached) {
    return cached
  }
  const sessions = await queryClient.fetchQuery({
    queryKey: queryKeys.sessions,
    queryFn: () => getSessions(),
    staleTime: SESSIONS_STALE_TIME,
  })
  return sessions ?? []
}

/** 强制重拉会话列表（无视缓存新鲜度，结果回写缓存）。
 *  供"必须新鲜"的判定路径用：管道出生不触发会话列表失效（无事件源），
 *  缓存可能任意陈旧——管道跳转兜底查归属必须绕过缓存，否则任务运行期
 *  用缓存重查等于没查（必报"找不到该管道"）。 */
export async function forceReloadSessions(): Promise<Session[]> {
  const sessions = await queryClient.fetchQuery({
    queryKey: queryKeys.sessions,
    queryFn: () => getSessions(),
    staleTime: 0,
  })
  return sessions ?? []
}

/** 非组件环境读当前缓存的会话列表（无缓存返回空数组，调用方无需空值分支） */
export function readSessions(): Session[] {
  return queryClient.getQueryData<Session[]>(queryKeys.sessions) ?? []
}

/** 更新缓存的会话列表（乐观更新/写操作回填共用；无缓存视同 []） */
export function updateSessionsCache(updater: (prev: Session[]) => Session[]): void {
  queryClient.setQueryData<Session[]>(queryKeys.sessions, (prev) => updater(prev ?? []))
}

/** 标记会话列表缓存失效（下次活跃订阅触发后台重拉；create/delete 后用） */
export function invalidateSessions(): void {
  void queryClient.invalidateQueries({ queryKey: queryKeys.sessions })
}
