/**
 * 调试任务页面
 *
 * 展示任务列表，支持按状态过滤
 */

import { useState, useEffect, useCallback } from 'react'
import { getTaskList } from '@/services/api/monitoring'
import type { TaskInfo } from '@/types/monitoring'

/** 任务状态选项 */
const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'pending', label: '等待中' },
  { value: 'running', label: '运行中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

/**
 * 获取任务状态样式
 */
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
    case 'cancelled':
      return 'bg-status-pending/10 text-status-pending'
    default:
      return 'bg-status-pending/10 text-status-pending'
  }
}

/**
 * 调试任务页面组件
 */
export function DebugTasksPage() {
  const [tasks, setTasks] = useState<TaskInfo[]>([])
  const [total, setTotal] = useState(0)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [statusFilter, setStatusFilter] = useState('')
  const [page, setPage] = useState(1)
  const pageSize = 20

  /**
   * 加载任务列表
   */
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

  /** 状态过滤变更 */
  const handleStatusChange = (status: string) => {
    setStatusFilter(status)
    setPage(1)
    fetchTasks(1, status || undefined)
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/debug" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试任务</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {total} 个任务</span>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-3 sm:p-6">
        {/* 状态过滤 */}
        <div className="flex gap-2">
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
        {isLoading && (
          <div className="flex items-center justify-center py-12">
            <div className="border-primary h-6 w-6 animate-spin rounded-full border-2 border-t-transparent" />
            <span className="text-muted-foreground ml-2 text-sm">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && tasks.length === 0 && (
          <div className="text-muted-foreground py-12 text-center">暂无数据</div>
        )}

        {/* 任务列表 */}
        {!isLoading && !error && tasks.length > 0 && (
          <>
            {/* 移动端卡片视图 */}
            <div className="space-y-2 md:hidden">
              {tasks.map((task) => (
                <div key={task.id} className="rounded-lg border p-3">
                  <div className="flex items-start justify-between gap-2">
                    <span className="text-sm font-medium leading-snug">{task.intent || task.name || task.id}</span>
                    <span
                      className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${getTaskStatusStyle(task.status)}`}
                    >
                      {task.status}
                    </span>
                  </div>
                  <div className="text-muted-foreground mt-2 space-y-1 text-xs">
                    <div>当前步骤：{task.current_step || '--'}</div>
                    <div>创建时间：{new Date(task.created_at).toLocaleString()}</div>
                    <div>耗时：{task.duration ? `${(task.duration / 1000).toFixed(1)}s` : '--'}</div>
                  </div>
                </div>
              ))}
            </div>
            {/* 桌面端表格视图 */}
            <div className="hidden md:block overflow-hidden rounded-lg border">
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
                      当前步骤
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      创建时间
                    </th>
                    <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                      耗时
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map((task) => (
                    <tr key={task.id} className="hover:bg-accent/20 border-t">
                      <td className="max-w-[200px] truncate px-4 py-2">
                        {task.intent || task.name || task.id}
                      </td>
                      <td className="px-4 py-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs ${getTaskStatusStyle(task.status)}`}
                        >
                          {task.status}
                        </span>
                      </td>
                      <td className="text-muted-foreground max-w-[150px] truncate px-4 py-2 text-xs">
                        {task.current_step || '--'}
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {new Date(task.created_at).toLocaleString()}
                      </td>
                      <td className="text-muted-foreground px-4 py-2 text-xs">
                        {task.duration ? `${(task.duration / 1000).toFixed(1)}s` : '--'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 分页 */}
            {totalPages > 1 && (
              <div className="flex items-center justify-center gap-2 pt-2">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="hover:bg-accent/50 min-h-[44px] rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  上一页
                </button>
                <span className="text-muted-foreground text-sm">
                  {page} / {totalPages}
                </span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="hover:bg-accent/50 min-h-[44px] rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
                >
                  下一页
                </button>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  )
}
