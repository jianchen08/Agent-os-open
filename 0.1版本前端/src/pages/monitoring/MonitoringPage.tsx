/**
 * 系统监控仪表盘页面
 *
 * 展示系统指标、任务统计和最近任务列表，支持自动刷新
 */

import { Activity } from 'lucide-react'
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
      return 'bg-status-success/10 text-status-success'
    case 'running':
      return 'bg-status-info/10 text-status-info'
    case 'failed':
      return 'bg-status-error/10 text-status-error'
    case 'pending':
      return 'bg-status-warning/10 text-status-warning'
    case 'cancelled':
      return 'bg-muted-foreground/10 text-muted-foreground'
    default:
      return 'bg-muted-foreground/10 text-muted-foreground'
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
    apiTokenUsage,
    cacheStats,
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
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回
        </a>
        <h1 className="ml-4 text-base font-semibold">系统监控</h1>
        <div className="ml-auto flex items-center gap-3">
          {lastUpdated && (
            <span className="text-muted-foreground hidden text-xs sm:inline">
              更新于 {new Date(lastUpdated).toLocaleTimeString()}
            </span>
          )}
          <label className="text-muted-foreground flex cursor-pointer items-center gap-1.5 text-xs">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded"
            />
            <span className="hidden sm:inline">自动刷新</span>
          </label>
          <button
            onClick={handleRefresh}
            disabled={isLoading || localRefreshing}
            className="hover:bg-accent/50 flex items-center gap-1 rounded-lg border px-3 py-1 text-xs disabled:opacity-50"
            aria-label="刷新监控数据"
          >
            {isLoading || localRefreshing ? '刷新中...' : '刷新'}
          </button>
        </div>
      </header>
      <main className="flex-1 space-y-6 overflow-y-auto p-3 sm:p-6">
        {/* 错误提示 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 加载状态 - 骨架屏 */}
        {isLoading && !metrics && !statistics && (
          <>
            {/* 系统指标骨架 */}
            <section>
              <h2 className="mb-3 text-sm font-semibold">系统指标</h2>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
                {Array.from({ length: 4 }).map((_, i) => (
                  <div key={i} className="animate-pulse rounded-lg border p-4">
                    <div className="bg-muted mb-1 h-3 w-20 rounded" />
                    <div className="bg-muted h-6 w-16 rounded" />
                  </div>
                ))}
              </div>
            </section>
            {/* 任务统计骨架 */}
            <section>
              <h2 className="mb-3 text-sm font-semibold">任务统计</h2>
              <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
                {Array.from({ length: 5 }).map((_, i) => (
                  <div key={i} className="animate-pulse rounded-lg border p-4">
                    <div className="bg-muted mb-1 h-3 w-16 rounded" />
                    <div className="bg-muted h-6 w-12 rounded" />
                  </div>
                ))}
              </div>
            </section>
          </>
        )}

        {/* 系统指标 */}
        {metrics && (
          <section>
            <h2 className="mb-3 text-sm font-semibold">系统指标</h2>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">CPU 使用率</div>
                <div className="text-xl font-semibold">{metrics.cpu_usage.toFixed(1)}%</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">内存使用</div>
                <div className="text-xl font-semibold">
                  {metrics.memory.usage_percent.toFixed(1)}%
                </div>
                <div className="text-muted-foreground mt-1 text-xs">
                  {formatBytes(metrics.memory.used)} / {formatBytes(metrics.memory.total)}
                </div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">磁盘使用</div>
                <div className="text-xl font-semibold">
                  {metrics.disk.usage_percent.toFixed(1)}%
                </div>
                <div className="text-muted-foreground mt-1 text-xs">
                  {formatBytes(metrics.disk.used)} / {formatBytes(metrics.disk.total)}
                </div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">运行时间</div>
                <div className="text-xl font-semibold">
                  {metrics.uptime ? formatUptime(metrics.uptime) : '--'}
                </div>
              </div>
            </div>
          </section>
        )}

        {/* Token 统计 */}
        {(apiTokenUsage || cacheStats) && (
          <section>
            <h2 className="mb-3 text-sm font-semibold">Token 统计</h2>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
              {apiTokenUsage && (
                <>
                  <div className="rounded-lg border p-4">
                    <div className="text-muted-foreground mb-1 text-xs">总 Token 使用量</div>
                    <div className="text-xl font-semibold">
                      {apiTokenUsage.total_tokens.toLocaleString()}
                    </div>
                    <div className="text-muted-foreground mt-1 text-xs">
                      {apiTokenUsage.request_count.toLocaleString()} 次请求
                    </div>
                  </div>
                  <div className="rounded-lg border p-4">
                    <div className="text-muted-foreground mb-1 text-xs">输入 Token 数</div>
                    <div className="text-xl font-semibold">
                      {apiTokenUsage.prompt_tokens.toLocaleString()}
                    </div>
                  </div>
                  <div className="rounded-lg border p-4">
                    <div className="text-muted-foreground mb-1 text-xs">输出 Token 数</div>
                    <div className="text-xl font-semibold">
                      {apiTokenUsage.completion_tokens.toLocaleString()}
                    </div>
                  </div>
                </>
              )}
              {cacheStats && (
                <div className="rounded-lg border p-4">
                  <div className="text-muted-foreground mb-1 text-xs">缓存命中率</div>
                  <div className="text-xl font-semibold">
                    {cacheStats.hit_rate.toFixed(1)}%
                  </div>
                  <div className="text-muted-foreground mt-1 text-xs">
                    命中 {cacheStats.cache_hits.toLocaleString()} / 未命中 {cacheStats.cache_misses.toLocaleString()} / 共 {cacheStats.total_requests.toLocaleString()} 次
                  </div>
                </div>
              )}
            </div>
          </section>
        )}

        {/* 任务统计 */}
        {statistics && (
          <section>
            <h2 className="mb-3 text-sm font-semibold">任务统计</h2>
            <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">总任务数</div>
                <div className="text-xl font-semibold">{statistics.total}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">已完成</div>
                <div className="text-xl font-semibold text-status-success">{statistics.succeeded}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">失败</div>
                <div className="text-xl font-semibold text-status-error">{statistics.failed}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">运行中</div>
                <div className="text-xl font-semibold text-status-info">{statistics.running}</div>
              </div>
              <div className="rounded-lg border p-4">
                <div className="text-muted-foreground mb-1 text-xs">成功率</div>
                <div className="text-xl font-semibold">{statistics.success_rate.toFixed(1)}%</div>
              </div>
            </div>
          </section>
        )}

        {/* 最近任务 */}
        <section>
          <h2 className="mb-3 text-sm font-semibold">最近任务</h2>
          {recentTasks.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12">
              <Activity className="text-muted-foreground/40 mb-3 h-10 w-10" />
              <p className="text-muted-foreground text-sm">暂无任务记录</p>
              <p className="text-muted-foreground/60 mt-1 text-xs">当有任务执行时，这里会显示最近的任务</p>
            </div>
          ) : (
            <>
              {/* 移动端卡片视图 */}
              <div className="space-y-2 md:hidden">
                {recentTasks.map((task) => (
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
                        创建时间
                      </th>
                      <th className="text-muted-foreground px-4 py-2 text-left text-xs font-medium">
                        耗时
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentTasks.map((task) => (
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
            </>
          )}
        </section>
      </main>
    </div>
  )
}
