/** 调试任务页面 展示任务列表，支持按状态过滤，支持暂停任务的恢复操作 */

import { useState, useEffect, useCallback } from 'react'
import { Play } from '@/assets/icons'
import { ErrorState } from '@/components/shared/ErrorState'
import { LoadingState } from '@/components/shared/LoadingState'
import { PageShell } from '@/components/shared/PageShell'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { getTaskList } from '@/services/api/monitoring'
import { resumeTask } from '@/services/api/tasks'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import type { TaskInfo } from '@/types/monitoring'

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
      return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400'
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
export function DebugTasksPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const [resumingIds, setResumingIds] = useState<Set<string>>(new Set())
  const pageSize = 20

  /** 加载任务列表 */
  const fetchTasks = useCallback(async (p: number, status?: string) => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await getTaskList(p, pageSize, status || undefined)
      setTasks(res.items)
      setTotal(res.total)
    } catch (err: any) {
      setError(err.message || '获取任务列表失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchTasks(page)
  }, [page, fetchTasks])

  /** 监听任务状态变更 WS 事件，自动刷新当前列表 */
  useEffect(() => {
    const handleStatusChange = () => {
      fetchTasks(page, statusFilter || undefined)
    }
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleStatusChange as any)
    globalWS.subscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleStatusChange as any)
    return () => {
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_CHANGED, handleStatusChange as any)
      globalWS.unsubscribe(WS_SERVER_EVENTS.TASK_STATUS_UPDATE, handleStatusChange as any)
    }
  }, [fetchTasks, page, statusFilter])

  /** 状态过滤变更 */
  const handleStatusChange = (status: string) => {
    setStatusFilter(status)
    setPage(1)
    fetchTasks(1, status || undefined)
  }

  /** 恢复任务 */
  const handleResume = async (taskId: string) => {
    setResumingIds((prev) => new Set(prev).add(taskId))
    try {
      await resumeTask(taskId)
      fetchTasks(page, statusFilter || undefined)
    } catch (err: any) {
      setError(err.message || '恢复任务失败')
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
                      说明
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
                    return (
                      <tr
                        key={task.id}
                        className="border-t hover:bg-accent/20"
                      >
                        <td className="max-w-[200px] truncate px-4 py-2">
                          {task.intent || task.name || task.id}
                        </td>
                        <td className="px-4 py-2">
                          <span
                            className={`rounded-full px-2 py-0.5 text-xs transition-colors duration-300 ${getTaskStatusStyle(task.status)}`}
                          >
                            {getTaskStatusLabel(task.status)}
                          </span>
                        </td>
                        <td className="text-muted-foreground max-w-[200px] truncate px-4 py-2 text-xs">
                          {task.error || task.description || task.current_step || '--'}
                        </td>
                        <td className="text-muted-foreground px-4 py-2 text-xs">
                          {new Date(task.created_at).toLocaleString()}
                        </td>
                        <td className="px-4 py-2">
                          {task.status === 'suspended' && (
                            <button
                              onClick={() => handleResume(task.id)}
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
