/**
 * 调试中心 query 集（服务端状态 query 化）
 *
 * 调试中心 6 个低频页面的「mount 即拉、零缓存」改为 stale-while-revalidate：
 * - 静态 key（debugTasks/debugSessions/evaluationMetrics/debugUsers/dbTables）
 *   同一缓存槽：staleTime 60s 窗口内重挂零请求，过期后台静默刷新
 * - 带参 key（executionRecords(sessionId) / llmPayloadDiag(page)）参数变 = 缓存条目变
 * - 分页/过滤类（tasks 页码+状态、evaluationMetrics 分类）key 无工厂参数，
 *   参数变化用显式 refetch 重拉（静态 key 槽缓存最新一页，SWR 语义不变）
 * - 例外：llmPayloadDiag 快照随每次 LLM 调用实时增长，staleTime 0 挂载即重取
 *   （缓存仅用于先渲染不闪 loading），否则列表恒落后实际发送一轮
 *
 * staleTime 统一 60_000：调试/监控类数据允许分钟级陈旧，后台静默刷新。
 */

import { useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { fetchDbTables } from '@/services/api/dbAdmin'
import { getEvaluationMetrics } from '@/services/api/evaluationMetrics'
import { getExecutionRecords, getExecutionRecordsSessions } from '@/services/api/executionRecords'
import { getPayloadDiagList } from '@/services/api/llmPayload'
import { getTaskList } from '@/services/api/monitoring'
import { getUsers } from '@/services/api/users'
import { queryKeys } from '@/services/query/queryKeys'

/** 调试/监控类数据新鲜窗口：数据允许分钟级陈旧，过期后台静默刷新 */
const DEBUG_STALE_TIME = 60_000

/** ── 任务列表（静态 key + 页码/状态变化显式重拉） ── */

export interface DebugTasksParams {
  /** 页码（1 起） */
  page: number
  /** 每页条数 */
  pageSize?: number
  /** 状态过滤（空串=全部） */
  status?: string
}

export function useDebugTasksQuery({ page, pageSize = 20, status }: DebugTasksParams) {
  const query = useQuery({
    queryKey: queryKeys.debugTasks,
    // 箭头包裹：隔离 getTaskList 的 RetryOptions 可选参
    queryFn: () => getTaskList(page, pageSize, status || undefined),
    staleTime: DEBUG_STALE_TIME,
  })

  // 页码/状态过滤变化 → 显式重拉（staleTime 窗口内挂载重挂零请求，但翻页必须重拉）。
  // 用「参数比对」而非首次运行标记：React 19 StrictMode 下 effect 双跑同参不重拉。
  const lastParams = useRef<{ page: number; status?: string } | null>(null)
  useEffect(() => {
    const prev = lastParams.current
    lastParams.current = { page, status }
    if (prev && (prev.page !== page || prev.status !== status)) {
      void query.refetch()
    }
  }, [page, status])

  return query
}

/** ── 调试会话列表（静态 key） ── */

export function useDebugSessionsQuery() {
  return useQuery({
    queryKey: queryKeys.debugSessions,
    // 箭头包裹：隔离可能的可选参，避免 QueryFunctionContext 误传入
    queryFn: () => getExecutionRecordsSessions(),
    staleTime: DEBUG_STALE_TIME,
  })
}

/** ── 执行记录（按会话分条缓存：sessionId 变 = 缓存条目变） ── */
export function useExecutionRecordsQuery(sessionId?: string) {
  return useQuery({
    queryKey: queryKeys.executionRecords(sessionId),
    queryFn: () =>
      getExecutionRecords({
        session_id: sessionId || undefined,
        limit: 50,
      }),
    staleTime: DEBUG_STALE_TIME,
  })
}

/** ── 评估指标（分类过滤变化显式重拉，静态 key 槽缓存） ── */
export function useEvaluationMetricsQuery(category?: string) {
  const query = useQuery({
    queryKey: queryKeys.evaluationMetrics,
    queryFn: () =>
      getEvaluationMetrics({
        category: category || undefined,
        limit: 100,
      }),
    staleTime: DEBUG_STALE_TIME,
  })

  // 分类过滤变化 → 显式重拉（StrictMode 双跑 effect 下参数未变不重拉）
  const lastCategory = useRef<string | null>(null)
  useEffect(() => {
    const prev = lastCategory.current
    lastCategory.current = category ?? null
    if (prev !== undefined && prev !== (category ?? null)) {
      void query.refetch()
    }
  }, [category])

  return query
}

/** ── 调试用户列表 ── */
export function useDebugUsersQuery() {
  return useQuery({
    queryKey: queryKeys.debugUsers,
    // 箭头包裹：getUsers 带 skip/limit 可选参
    queryFn: () => getUsers(),
    staleTime: DEBUG_STALE_TIME,
  })
}

/** ── LLM 请求诊断列表（按页分条缓存；当前 UI 无分页，恒第 1 页） ── */
export function useLlmPayloadDiagQuery(page = 1) {
  return useQuery({
    queryKey: queryKeys.llmPayloadDiag(page),
    queryFn: () => getPayloadDiagList(),
    // 快照随每次 LLM 调用实时落盘，是本组页面中唯一随聊天高频增长的数据：
    // 不适用 60s SWR 窗口（无 WS invalidate / 无轮询兜底，窗口内重进页面
    // 零请求会恒显示上一轮列表）。staleTime 0 = 挂载即后台重取，缓存仅用于
    // 先渲染不闪 loading。
    staleTime: 0,
  })
}

/** ── DB 管理：表列表（admin 守卫通过前不发起请求） ── */
export function useDbTablesQuery(options?: { enabled?: boolean }) {
  return useQuery({
    queryKey: queryKeys.dbTables,
    queryFn: () => fetchDbTables(),
    staleTime: DEBUG_STALE_TIME,
    enabled: options?.enabled,
  })
}
