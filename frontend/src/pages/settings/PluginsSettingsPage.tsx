/**
 * 插件管理设置页面
 *
 * 展示所有插件状态，支持热重载单个/全部插件，查看重载历史
 */

import { RefreshCw, Zap, AlertCircle, ChevronDown, ChevronRight, Plug } from 'lucide-react'
import { useState, useEffect, useCallback } from 'react'
import apiClient from '@/services/api/client'

/** 插件状态信息 */
interface PluginStatus {
  config_path: string
  config_type: string
  plugin_id: string
  status: string
  last_load_time: string | null
  error: string | null
  version: string | null
}

/** 重载事件 */
interface ReloadEvent {
  config_path: string
  config_type: string
  event_type: string
  success: boolean
  error: string | null
  rolled_back: boolean
  timestamp?: string
}

/** 单个重载结果 */
interface ReloadResult {
  config_path: string
  config_type: string
  success: boolean
  error: string | null
  rolled_back: boolean
}

/**
 * 获取状态标签样式
 *
 * Args:
 *   status: 插件状态字符串
 *
 * Returns:
 *   Tailwind CSS 类名字符串
 */
function getStatusStyle(status: string): string {
  switch (status) {
    case 'loaded':
      return 'bg-status-success/10 text-status-success'
    case 'error':
      return 'bg-status-error/10 text-status-error'
    default:
      return 'bg-status-warning/10 text-status-warning'
  }
}

/**
 * 插件管理设置页面组件
 */
export function PluginsSettingsPage() {
  const [plugins, setPlugins] = useState<PluginStatus[]>([])
  const [history, setHistory] = useState<ReloadEvent[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [isReloading, setIsReloading] = useState<string | null>(null)
  const [isReloadingAll, setIsReloadingAll] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [actionMessage, setActionMessage] = useState<string | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  /**
   * 加载插件状态列表
   */
  const fetchPlugins = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await apiClient.get<PluginStatus[]>('/api/v1/plugins/status')
      setPlugins(res.data)
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '获取插件状态失败'
      setError(message)
    } finally {
      setIsLoading(false)
    }
  }, [])

  /**
   * 加载重载历史
   */
  const fetchHistory = useCallback(async () => {
    try {
      const res = await apiClient.get<ReloadEvent[]>('/api/v1/plugins/history', {
        params: { limit: 50 },
      })
      setHistory(res.data)
    } catch {
      // 历史加载失败不阻塞页面
    }
  }, [])

  useEffect(() => {
    fetchPlugins()
    fetchHistory()
  }, [fetchPlugins, fetchHistory])

  /**
   * 重载指定插件
   *
   * Args:
   *   configPath: 插件配置路径
   */
  const handleReload = async (configPath: string) => {
    setIsReloading(configPath)
    setActionMessage(null)
    try {
      const res = await apiClient.post<ReloadResult>('/api/v1/plugins/reload', null, {
        params: { config_path: configPath },
      })
      const result = res.data
      if (result.success) {
        setActionMessage(`插件 ${configPath} 重载成功`)
      } else {
        setActionMessage(`插件 ${configPath} 重载失败: ${result.error || '未知错误'}`)
      }
      await fetchPlugins()
      await fetchHistory()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '重载失败'
      setActionMessage(message)
    } finally {
      setIsReloading(null)
    }
  }

  /**
   * 重载全部插件
   */
  const handleReloadAll = async () => {
    setIsReloadingAll(true)
    setActionMessage(null)
    try {
      const res = await apiClient.post<ReloadResult[]>('/api/v1/plugins/reload-all')
      const results = res.data
      const successCount = results.filter((r) => r.success).length
      const failCount = results.length - successCount
      setActionMessage(
        `全部重载完成: ${successCount} 成功${failCount > 0 ? `, ${failCount} 失败` : ''}`,
      )
      await fetchPlugins()
      await fetchHistory()
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : '全部重载失败'
      setActionMessage(message)
    } finally {
      setIsReloadingAll(false)
    }
  }

  return (
    <div className="bg-background text-foreground flex h-screen flex-col overflow-hidden">
      <header className="flex h-12 shrink-0 items-center border-b px-4">
        <a href="/settings" className="text-muted-foreground hover:text-foreground text-sm">
          &larr; 返回设置
        </a>
        <h1 className="ml-4 text-base font-semibold">插件管理</h1>
        <span className="text-muted-foreground ml-auto text-xs">共 {plugins.length} 个插件</span>
      </header>
      <main className="flex-1 space-y-4 overflow-y-auto p-6">
        {/* 操作按钮 */}
        <div className="flex items-center gap-3">
          <button
            onClick={handleReloadAll}
            disabled={isReloadingAll || isLoading}
            className="bg-primary text-primary-foreground flex items-center gap-1.5 rounded-lg px-4 py-1.5 text-sm hover:opacity-90 disabled:opacity-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${isReloadingAll ? 'animate-spin' : ''}`} />
            {isReloadingAll ? '重载中...' : '全部重载'}
          </button>
          <button
            onClick={() => {
              fetchPlugins()
              fetchHistory()
            }}
            className="hover:bg-accent/50 rounded-lg border px-3 py-1.5 text-sm"
          >
            刷新状态
          </button>
        </div>

        {/* 操作结果提示 */}
        {actionMessage && (
          <div
            className={`rounded-lg p-3 text-sm ${
              actionMessage.includes('失败')
                ? 'bg-destructive/10 text-destructive'
                : 'bg-status-success/10 text-status-success'
            }`}
          >
            {actionMessage}
          </div>
        )}

        {/* 错误状态 */}
        {error && (
          <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>
        )}

        {/* 加载状态 - 骨架屏 */}
        {isLoading && (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="animate-pulse rounded-lg border p-4">
                <div className="mb-2 flex items-start justify-between">
                  <div className="bg-muted h-4 w-2/3 rounded" />
                  <div className="bg-muted h-5 w-12 rounded-full" />
                </div>
                <div className="bg-muted mb-2 h-3 w-full rounded" />
                <div className="bg-muted h-3 w-4/5 rounded" />
              </div>
            ))}
          </div>
        )}

        {/* 空状态 */}
        {!isLoading && !error && plugins.length === 0 && (
          <div className="flex flex-col items-center justify-center py-16">
            <Plug className="text-muted-foreground/40 mb-3 h-12 w-12" />
            <p className="text-muted-foreground text-sm">暂无已注册的插件</p>
            <p className="text-muted-foreground/60 mt-1 text-xs">
              请在 config/ 目录下添加 YAML 配置文件
            </p>
          </div>
        )}

        {/* 插件状态列表 */}
        {!isLoading && !error && plugins.length > 0 && (
          <div
            className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3"
            aria-live="polite"
            aria-label="插件列表"
          >
            {plugins.map((plugin) => (
              <div key={plugin.config_path} className="rounded-lg border p-4">
                <div className="mb-2 flex items-start justify-between">
                  <h3 className="mr-2 flex-1 truncate text-sm font-semibold" title={plugin.config_path}>
                    {plugin.plugin_id || plugin.config_path}
                  </h3>
                  <span
                    className={`shrink-0 rounded-full px-2 py-0.5 text-xs ${getStatusStyle(plugin.status)}`}
                  >
                    {plugin.status}
                  </span>
                </div>

                <div className="text-muted-foreground mb-2 space-y-1 text-xs">
                  <div className="truncate" title={plugin.config_path}>
                    路径: {plugin.config_path}
                  </div>
                  <div>类型: {plugin.config_type}</div>
                  {plugin.version && <div>版本: {plugin.version}</div>}
                  {plugin.last_load_time && (
                    <div>
                      上次加载:{' '}
                      {new Date(plugin.last_load_time).toLocaleString()}
                    </div>
                  )}
                </div>

                {plugin.error && (
                  <div className="bg-status-error/10 text-status-error mb-2 flex items-start gap-1 rounded p-2 text-xs">
                    <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                    <span className="line-clamp-2">{plugin.error}</span>
                  </div>
                )}

                <button
                  onClick={() => handleReload(plugin.config_path)}
                  disabled={isReloading === plugin.config_path}
                  className="hover:bg-accent/50 flex w-full items-center justify-center gap-1.5 rounded border px-3 py-1.5 text-xs disabled:opacity-50"
                >
                  <RefreshCw
                    className={`h-3 w-3 ${isReloading === plugin.config_path ? 'animate-spin' : ''}`}
                  />
                  {isReloading === plugin.config_path ? '重载中...' : '重载'}
                </button>
              </div>
            ))}
          </div>
        )}

        {/* 重载历史 - 折叠面板 */}
        {!isLoading && (
          <div className="rounded-lg border">
            <button
              onClick={() => setShowHistory(!showHistory)}
              className="hover:bg-accent/30 flex w-full items-center justify-between p-4 text-sm transition-colors"
            >
              <span className="flex items-center gap-2 font-medium">
                <Zap className="h-4 w-4" />
                重载历史
                {history.length > 0 && (
                  <span className="text-muted-foreground text-xs">({history.length})</span>
                )}
              </span>
              {showHistory ? (
                <ChevronDown className="h-4 w-4" />
              ) : (
                <ChevronRight className="h-4 w-4" />
              )}
            </button>

            {showHistory && (
              <div className="border-t">
                {history.length === 0 ? (
                  <div className="text-muted-foreground p-4 text-center text-xs">
                    暂无重载记录
                  </div>
                ) : (
                  <div className="divide-y">
                    {history.map((evt, idx) => (
                      <div key={`${evt.config_path}-${idx}`} className="flex items-center gap-3 p-3 text-xs">
                        <span
                          className={`h-2 w-2 shrink-0 rounded-full ${
                            evt.success ? 'bg-status-success' : 'bg-status-error'
                          }`}
                        />
                        <span className="flex-1 truncate">{evt.config_path}</span>
                        <span className="text-muted-foreground shrink-0">
                          {evt.event_type || evt.config_type}
                        </span>
                        {evt.error && (
                          <span className="text-status-error truncate" title={evt.error}>
                            {evt.error}
                          </span>
                        )}
                        {evt.rolled_back && (
                          <span className="bg-status-warning/10 text-status-warning rounded px-1.5 py-0.5 text-xs">
                            已回滚
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  )
}
