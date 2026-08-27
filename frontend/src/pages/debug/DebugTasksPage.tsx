/** 调试任务页面（query 化：useDebugTasksQuery 缓存 SWR，重挂零请求） */

import { useEffect, useState } from 'react'
import { Play } from '@/assets/icons'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { resumeTask } from '@/services/api/tasks'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { useDebugTasksQuery } from '@/hooks/queries/useDebugQueries'

/** 任务状态选项 */
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '等待中' },
  { value: 'running', label: '运行中' },
  { value: 'suspended', label: '已暂停' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

/** 获取任务状态样式 */
function getTaskStatusStyle(status: string): string {
  switch (status) {
    case 'completed':
      return 'bg-status-success/10 text-status-success'
    case 'running':
      return 'bg-status-info/10 text-status-info'
    case 'failed':
      return 'bg-status-error/10 text-status-error'
    case 'pending':
      return 'bg-status-warning/10 text-status-warning'
    case 'suspended':
      return 'bg-status-warning/10 text-status-warning dark:bg-status-warning/20'
    case 'cancelled':
      return 'bg-status-pending/10 text-status-pending'
    default:
      return 'bg-status-pending/10 text-status-pending'
  }
}

/** 获取任务状态的中文标签 */
function getTaskStatusLabel(status: string): string {
  switch (status) {
    case 'pending': return '等待中'
    case 'running': return '运行中'
    case 'suspended': return '已暂停'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    case 'cancelled': return '已取消'
    default: return status
  }
}

/** 调试任务页面组件 */
export function DebugTasksPage({ embedded }: { embedded?: boolean } = {}) {
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [resumingIds, setResumingIds] = useState<Set<string>>(new Set())
  // 恢复操作错误（查询错误走 tasksQuery.error）
  const [actionError, setActionError] = useState<string | null>(null)
  const pageSize = 20

  // 任务列表（query 化）：staleTime 60s 窗口内重挂零请求；页码/状态变化显式重拉
  const tasksQuery = useDebugTasksQuery({ page, pageSize, status: statusFilter })
  const tasks = tasksQuery.data?.items ?? []
  const total = tasksQuery.data?.total ?? 0
  // 无缓存数据时显示 loading（有缓存先渲染缓存不闪 loading）
  const isLoading = tasksQuery.isPending && !tasksQuery.data
  const error = tasksQuery.isError
    ? tasksQuery.error instanceof Error
      ? tasksQuery.error.message
      : '获取任务列表失败'
    : actionError

  /** 监听任务状态变更 WS 事件，自动刷新当前列表 */
  useEffect(() => {
    const handleStatusChange = () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.debugTasks })
    }
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleStatusChange)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleStatusChange)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleStatusChange)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleStatusChange)
    }
  }, [])

  /** 状态过滤变更 */
  const handleStatusChange = (status: string) => {
    setStatusFilter(status)
    setPage(1)
    setActionError(null)
  }

  /** 恢复任务 */
  const handleResume = async (taskId: string) => {
    setResumingIds((prev) => new Set(prev).add(taskId))
    try {
      await resumeTask(taskId)
      void queryClient.invalidateQueries({ queryKey: queryKeys.debugTasks })
    } catch (err: unknown) {
      setActionError(err instanceof Error ? err.message : '恢复任务失败')
    } finally {
      setResumingIds((prev) => {
        const next = new Set(prev)
        next.delete(taskId)
        return next
      })
    }
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <PageShell
      title="调试任务"
      backHref="/debug"
      embedded={embedded}
      actions={<span className="text-muted-foreground text-xs">共 {total} 个任务</span>}
    >
      {/* 状态过滤 */}
      <div className="flex gap-2 flex-wrap">
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => handleStatusChange(opt.value)}
              className={`rounded-lg border px-3 py-1.5 text-xs transition-colors ${
                statusFilter === opt.value
                  ? 'bg-primary text-primary-foreground border-primary'
                  : 'hover:bg-accent/50'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>

        {/* 加载状态 */}
        {isLoading && <LoadingState />}

        {/* 错误提示 */}
        {error && <ErrorState message={error} />}

        {/* 空状态 */}
        {!isLoading && !error && tasks.length === 0 && (
          <div className="text-muted-foreground py-12 text-center">暂无数据</div>
        )}

        {/* 任务列表 */}
        {!isLoading && !error && tasks.length > 0 && (
          <>
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      任务
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      状态
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      Agent
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      阶段
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      关键数据
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      创建时间
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      操作
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => {
                    const hasValidTime = task.created_at && !Number.isNaN(new Date(task.created_at).getTime())
                    return (
                      <tr
                        key={task.id}
                        className="border-t hover:bg-accent/20"
                      >
                        <td className="max-w-[200px] truncate px-4 py-2">
                          {task.title || task.intent || task.name || task.id}
                        </td>
                        <td className="px-4 py-2">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs transition-colors duration-300 ${getTaskStatusStyle(task.status)}`}
                          >
                            {getTaskStatusLabel(task.status)}
                          </span>
                        </td>
                        <td className="text-muted-foreground max-w-[110px] truncate px-4 py-2 text-xs">
                          {task.agent_id || '--'}
                        </td>
                        <td className="text-muted-foreground max-w-[110px] truncate px-4 py-2 text-xs">
                          {task.error || task.current_phase || task.current_step || task.description || '--'}
                        </td>
                        <td className="text-muted-foreground px-4 py-2 text-xs whitespace-nowrap">
                          {task.message_count !== undefined && <span className="bg-accent/30 rounded px-1.5 py-0.5 mr-1">{task.message_count} 消息</span>}
                          {task.total_tokens !== undefined && task.total_tokens !== null && <span className="bg-accent/30 rounded px-1.5 py-0.5 mr-1">{task.total_tokens} tokens</span>}
                          {task.ended !== undefined && <span className="bg-accent/30 rounded px-1.5 py-0.5">{task.ended ? '已结束' : '进行中'}</span>}
                        </td>
                        <td className="text-muted-foreground px-4 py-2 text-xs">
                          {hasValidTime ? new Date(task.created_at).toLocaleString() : '--'}
                        </td>
                        <td className="px-4 py-2">
                          {task.status === 'suspended' && task.task_id && (
                            <button
                              onClick={() => handleResume(task.task_id as string)}
                              disabled={resumingIds.has(task.id)}
                              className="inline-flex items-center gap-1 rounded-lg bg-primary/10 text-primary hover:bg-primary/20 px-2.5 py-1 text-xs font-medium transition-colors disabled:opacity-50"
                            >
                              {resumingIds.has(task.id) ? (
                                <>
                                  <div className="h-3 w-3 animate-spin rounded-full border border-primary border-t-transparent" />
                                  恢复中...
                                </>
                              ) : (
                                <>
                                  <Play className="h-3 w-3" />
                                  恢复
                                </>
                              )}
                            </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {/* 分页 */}
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-2 pt-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  上一页
                </button>
                <span className="text-muted-foreground text-sm">
                  {page} / {totalPages}
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  下一页
                </button>
              </div>
            )}
          </>
        )}
    </PageShell>
  )
}
