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
      return 'bg-green-500/10 text-green-500'
    case 'running':
      return 'bg-blue-500/10 text-blue-500'
    case 'failed':
      return 'bg-red-500/10 text-red-500'
    case 'pending':
      return 'bg-yellow-500/10 text-yellow-500'
    case 'cancelled':
      return 'bg-gray-500/10 text-gray-500'
    default:
      return 'bg-gray-500/10 text-gray-500'
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
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/debug" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">调试任务</h1>
        <span className="ml-auto text-xs text-muted-foreground">共 {total} 个任务</span>
      </header>
      <main className="flex-1 overflow-y-auto p-6 space-y-4">
        {/* 状态过滤 */}
        <div className="flex gap-2">
          {STATUS_OPTIONS.map(opt => (
            <button
              key={opt.value}
              onClick={() => handleStatusChange(opt.value)}
              className={`px-3 py-1.5 text-xs rounded-lg border transition-colors ${
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
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 错误提示 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && tasks.length === 0 && (
          <div className="text-center py-12 text-muted-foreground">暂无数据</div>
        )}

        {/* 任务列表 */}
        {!isLoading && !error && tasks.length > 0 && (
          <>
            <div className="border rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">任务</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">状态</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">当前步骤</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {tasks.map(task => (
                    <tr key={task.id} className="border-t hover:bg-accent/20">
                      <td className="px-4 py-2 truncate max-w-[200px]">
                        {task.intent || task.name || task.id}
                      </td>
                      <td className="px-4 py-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full ${getTaskStatusStyle(task.status)}`}>
                          {task.status}
                        </span>
                      </td>
                      <td className="px-4 py-2 text-xs text-muted-foreground truncate max-w-[150px]">
                        {task.current_step || '--'}
                      </td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">
                        {new Date(task.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-2 text-xs text-muted-foreground">
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
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
                >
                  上一页
                </button>
                <span className="text-sm text-muted-foreground">{page} / {totalPages}</span>
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  className="px-3 py-1.5 text-sm border rounded-lg disabled:opacity-50 hover:bg-accent/50"
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
