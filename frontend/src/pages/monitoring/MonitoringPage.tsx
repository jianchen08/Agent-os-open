/**
 * 系统监控仪表盘页面
 *
 * 展示系统指标、任务统计和最近任务列表，支持自动刷新
 */

import { useState, useEffect } from 'react'
import { useMonitoringStore } from '@/stores/monitoringStore'

/**
 * 格式化字节为可读字符串
 */
function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const i = Math.floor(Math.log(bytes) / Math.log(1024))
  return `${(bytes / Math.pow(1024, i)).toFixed(1)} ${units[i]}`
}

/**
 * 格式化运行时间
 */
function formatUptime(seconds: number): string {
  const d = Math.floor(seconds / 86400)
  const h = Math.floor((seconds % 86400) / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (d > 0) return `${d}天 ${h}时 ${m}分`
  if (h > 0) return `${h}时 ${m}分`
  return `${m}分`
}

/**
 * 获取任务状态标签样式
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
 * 系统监控页面组件
 */
export function MonitoringPage() {
  const {
    metrics,
    statistics,
    recentTasks,
    isLoading,
    error,
    lastUpdated,
    autoRefresh,
    fetchMonitoringData,
    setAutoRefresh,
  } = useMonitoringStore()

  const [localRefreshing, setLocalRefreshing] = useState(false)

  useEffect(() => {
    fetchMonitoringData()
  }, [fetchMonitoringData])

  /** 手动刷新 */
  const handleRefresh = async () => {
    setLocalRefreshing(true)
    await fetchMonitoringData()
    setLocalRefreshing(false)
  }

  return (
    <div className="h-screen flex flex-col bg-background text-foreground overflow-hidden">
      <header className="h-12 border-b flex items-center px-4 shrink-0">
        <a href="/" className="text-sm text-muted-foreground hover:text-foreground">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">系统监控</h1>
        <div className="ml-auto flex items-center gap-3">
          {lastUpdated && (
            <span className="text-xs text-muted-foreground">
              更新于 {new Date(lastUpdated).toLocaleTimeString()}
            </span>
          )}
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={e => setAutoRefresh(e.target.checked)}
              className="rounded"
            />
            自动刷新
          </label>
          <button
            onClick={handleRefresh}
            disabled={isLoading || localRefreshing}
            className="px-3 py-1 text-xs border rounded-lg hover:bg-accent/50 disabled:opacity-50"
          >
            {(isLoading || localRefreshing) ? '刷新中...' : '刷新'}
          </button>
        </div>
      </header>
      <main className="p-6 space-y-6">
        {/* 错误提示 */}
        {error && (
          <div className="p-4 rounded-lg bg-destructive/10 text-destructive text-sm">{error}</div>
        )}

        {/* 加载状态 */}
        {isLoading && !metrics && !statistics && (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-primary border-t-transparent rounded-full animate-spin" />
            <span className="ml-2 text-sm text-muted-foreground">加载中...</span>
          </div>
        )}

        {/* 系统指标 */}
        {metrics && (
          <section>
            <h2 className="text-sm font-semibold mb-3">系统指标</h2>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">CPU 使用率</div>
                <div className="text-xl font-semibold">{metrics.cpu_usage.toFixed(1)}%</div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">内存使用</div>
                <div className="text-xl font-semibold">{metrics.memory.usage_percent.toFixed(1)}%</div>
                <div className="text-xs text-muted-foreground mt-1">
                  {formatBytes(metrics.memory.used)} / {formatBytes(metrics.memory.total)}
                </div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">磁盘使用</div>
                <div className="text-xl font-semibold">{metrics.disk.usage_percent.toFixed(1)}%</div>
                <div className="text-xs text-muted-foreground mt-1">
                  {formatBytes(metrics.disk.used)} / {formatBytes(metrics.disk.total)}
                </div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">运行时间</div>
                <div className="text-xl font-semibold">
                  {metrics.uptime ? formatUptime(metrics.uptime) : '--'}
                </div>
              </div>
            </div>
          </section>
        )}

        {/* 任务统计 */}
        {statistics && (
          <section>
            <h2 className="text-sm font-semibold mb-3">任务统计</h2>
            <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">总任务数</div>
                <div className="text-xl font-semibold">{statistics.total}</div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">已完成</div>
                <div className="text-xl font-semibold text-green-500">{statistics.succeeded}</div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">失败</div>
                <div className="text-xl font-semibold text-red-500">{statistics.failed}</div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">运行中</div>
                <div className="text-xl font-semibold text-blue-500">{statistics.running}</div>
              </div>
              <div className="p-4 border rounded-lg">
                <div className="text-xs text-muted-foreground mb-1">成功率</div>
                <div className="text-xl font-semibold">{statistics.success_rate.toFixed(1)}%</div>
              </div>
            </div>
          </section>
        )}

        {/* 最近任务 */}
        <section>
          <h2 className="text-sm font-semibold mb-3">最近任务</h2>
          {recentTasks.length === 0 ? (
            <div className="text-center py-8 text-sm text-muted-foreground">暂无数据</div>
          ) : (
            <div className="border rounded-lg overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-accent/30">
                  <tr>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">任务</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">状态</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">创建时间</th>
                    <th className="text-left px-4 py-2 text-xs font-medium text-muted-foreground">耗时</th>
                  </tr>
                </thead>
                <tbody>
                  {recentTasks.map(task => (
                    <tr key={task.id} className="border-t hover:bg-accent/20">
                      <td className="px-4 py-2 truncate max-w-[200px]">
                        {task.intent || task.name || task.id}
                      </td>
                      <td className="px-4 py-2">
                        <span className={`text-xs px-2 py-0.5 rounded-full ${getTaskStatusStyle(task.status)}`}>
                          {task.status}
                        </span>
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
          )}
        </section>
      </main>
    </div>
  )
}
