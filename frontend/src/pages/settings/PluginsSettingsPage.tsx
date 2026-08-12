/**
 * 插件管理设置页面（重做版）
 *
 * 对齐安装触发模型（docs/working/重要设计/插件安装与触发模型设计.md §七）：
 * - 已安装列表 + Enabled 开关（PUT /api/v1/plugins/{id}/enabled）
 * - 真实状态（active/disabled）+ activation 策略（eager/lazy/manual）
 * - 配置入口（有 config_files 的插件可跳 PluginConfigEditor）
 * - 类型徽标 + host_type + version + 能力标记（contributes/http_endpoints）
 */

import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { RefreshCw, AlertCircle, Plug, Settings, ToggleLeft } from '@/assets/icons'
import { PageShell } from '@/components/shared/PageShell'
import { toast } from '@/components/ui/sonner'
import apiClient from '@/services/api/client'

/** 插件状态信息（对齐后端 plugins_status_handler 返回） */
interface PluginStatus {
  plugin_id: string
  name: string
  config_type: string
  host_type: string
  version: string | null
  enabled: boolean
  activation: string
  status: string
  config_files: Array<{ id: string; label: string; path: string }>
  has_contributes: boolean
  has_http_endpoints: boolean
  error: string | null
}

/** 类型徽标样式 */
function typeBadge(configType: string): { label: string; className: string } {
  const t = (configType || '').toLowerCase()
  if (t.includes('pipeline')) {
    return { label: 'Pipeline', className: 'bg-[rgba(167,139,250,0.15)] text-[var(--ds-accent-ai,#A78BFA)] border-[rgba(167,139,250,0.35)]' }
  }
  if (t.includes('tool')) {
    return { label: 'Tool', className: 'bg-[rgba(34,211,238,0.12)] text-[var(--ds-accent-primary,#22D3EE)] border-[rgba(34,211,238,0.35)]' }
  }
  if (t.includes('system')) {
    return { label: 'System', className: 'bg-[rgba(96,165,250,0.12)] text-[var(--ds-status-info,#60A5FA)] border-[rgba(96,165,250,0.35)]' }
  }
  return { label: configType || 'Composite', className: 'bg-white/5 text-muted-foreground border-white/10' }
}

/** activation 中文 */
function activationLabel(a: string): string {
  return { eager: '启动即载', lazy: '按需载入', manual: '手动启动' }[a] || a
}

export function PluginsSettingsPage({
  embedded = false,
  onSelectPluginConfig,
}: {
  embedded?: boolean
  /** 点击「配置」时回调（内联切换到对应插件配置项，不跳路由） */
  onSelectPluginConfig?: (pluginId: string, fileId: string) => void
}) {
  const navigate = useNavigate()
  const [plugins, setPlugins] = useState<PluginStatus[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [filter, setFilter] = useState<'all' | 'pipeline' | 'tool' | 'system' | 'disabled'>('all')

  const fetchPlugins = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await apiClient.get<PluginStatus[]>('/api/v1/plugins/status')
      setPlugins(res.data)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '获取插件状态失败')
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchPlugins()
  }, [fetchPlugins])

  /** 切换插件启用状态 */
  const handleToggleEnabled = async (pluginId: string, currentEnabled: boolean) => {
    setTogglingId(pluginId)
    try {
      const res = await apiClient.put<{ success: boolean; message?: string; error?: string }>(
        `/api/v1/plugins/${pluginId}/enabled`,
        { enabled: !currentEnabled },
      )
      if (res.data.success) {
        // 本地更新状态（立即反映，重启后内核才真正生效）
        setPlugins((prev) =>
          prev.map((p) =>
            p.plugin_id === pluginId
              ? { ...p, enabled: !currentEnabled, status: !currentEnabled ? 'active' : 'disabled' }
              : p,
          ),
        )
        toast.success(res.data.message || `已${!currentEnabled ? '启用' : '禁用'} ${pluginId}`)
      } else {
        toast.error(res.data.error || '操作失败')
      }
    } catch (err: unknown) {
      toast.error(err instanceof Error ? err.message : '操作失败')
    } finally {
      setTogglingId(null)
    }
  }

  const disabledCount = plugins.filter((p) => !p.enabled).length
  const filtered = plugins.filter((p) => {
    if (filter === 'all') return true
    if (filter === 'disabled') return !p.enabled
    return (p.config_type || '').toLowerCase().includes(filter)
  })

  const filterTabs: Array<{ id: typeof filter; label: string }> = [
    { id: 'all', label: `全部 ${plugins.length}` },
    { id: 'system', label: 'System' },
    { id: 'pipeline', label: 'Pipeline' },
    { id: 'tool', label: 'Tool' },
    { id: 'disabled', label: `已禁用 ${disabledCount}` },
  ]

  const mainContent = (
    <div className="space-y-4">
      {/* 操作栏 */}
      <div className="flex flex-wrap items-center gap-3">
        <button
          onClick={fetchPlugins}
          disabled={isLoading}
          className="hover:bg-white/5 flex items-center gap-1.5 rounded-lg border px-3 py-1.5 text-sm disabled:opacity-50"
          style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isLoading ? 'animate-spin' : ''}`} />
          刷新
        </button>
        <div className="ml-auto flex flex-wrap gap-1">
          {filterTabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setFilter(tab.id)}
              className={`rounded-md px-2.5 py-1 text-[11px] transition-colors ${
                filter === tab.id ? 'text-[var(--ds-accent-primary,#22D3EE)]' : 'text-muted-foreground hover:text-foreground'
              }`}
              style={filter === tab.id ? { background: 'var(--ds-bg-elevated, #111C38)', boxShadow: 'inset 0 0 0 1px var(--ds-border-active, rgba(34,211,238,0.45))' } : undefined}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      {error && <div className="bg-destructive/10 text-destructive rounded-lg p-4 text-sm">{error}</div>}

      {/* 加载骨架 */}
      {isLoading && (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="animate-pulse rounded-lg border p-3.5">
              <div className="bg-muted mb-2 h-4 w-2/3 rounded" />
              <div className="bg-muted h-3 w-full rounded" />
            </div>
          ))}
        </div>
      )}

      {/* 空状态 */}
      {!isLoading && !error && plugins.length === 0 && (
        <div className="flex flex-col items-center justify-center py-16">
          <Plug className="text-muted-foreground/40 mb-3 h-12 w-12" />
          <p className="text-muted-foreground text-sm">暂无已注册的插件</p>
        </div>
      )}

      {/* 插件列表 */}
      {!isLoading && !error && plugins.length > 0 && (
        <div className="flex flex-col gap-2" aria-live="polite">
          {filtered.length === 0 && (
            <p className="text-muted-foreground py-8 text-center text-sm">当前滤签下无插件</p>
          )}
          {filtered.map((plugin) => {
            const badge = typeBadge(plugin.config_type)
            const hasConfig = plugin.config_files.length > 0
            return (
              <div
                key={plugin.plugin_id}
                className="rounded-[10px] border p-3.5"
                style={{
                  background: plugin.enabled ? 'var(--ds-bg-panel, #0A1226)' : 'rgba(148,163,184,0.04)',
                  borderColor: plugin.error ? 'rgba(248,113,113,0.55)' : 'var(--ds-border-subtle, rgba(148,163,184,0.12))',
                  opacity: plugin.enabled ? 1 : 0.6,
                }}
              >
                {/* 第一行：名称 + 徽标 + 状态 + 开关 */}
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <h3 className="text-foreground min-w-0 flex-1 truncate text-[13px] font-medium" title={plugin.plugin_id}>
                    {plugin.name || plugin.plugin_id}
                  </h3>
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${badge.className}`}>
                    {badge.label}
                  </span>
                  {/* 启用开关 */}
                  <button
                    onClick={() => handleToggleEnabled(plugin.plugin_id, plugin.enabled)}
                    disabled={togglingId === plugin.plugin_id}
                    className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 transition-colors duration-200 disabled:opacity-50 ${
                      plugin.enabled
                        ? 'bg-[var(--ds-accent-primary,#22D3EE)] border-[var(--ds-accent-primary,#22D3EE)]'
                        : 'bg-gray-600 border-gray-600'
                    }`}
                    title={plugin.enabled ? '点击禁用（重启生效）' : '点击启用（重启生效）'}
                  >
                    <span
                      className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white transition duration-200 ${
                        plugin.enabled ? 'translate-x-3.5' : 'translate-x-0'
                      }`}
                    />
                  </button>
                </div>

                {/* 第二行：元信息（不重复显示 plugin_id，标题已有 name） */}
                <div className="text-muted-foreground mb-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10px]">
                  {plugin.version && <span>v{plugin.version}</span>}
                  <span className="text-[var(--ds-accent-primary,#22D3EE)]">{activationLabel(plugin.activation)}</span>
                  <span>{plugin.host_type}</span>
                  {plugin.has_contributes && <span title="有 UI 贡献">🎨 界面贡献</span>}
                  {plugin.has_http_endpoints && <span title="有 HTTP 端点">🌐 端点</span>}
                </div>

                {/* 错误 */}
                {plugin.error && (
                  <div className="bg-status-error/10 text-status-error mb-2 flex items-start gap-1 rounded p-2 text-xs">
                    <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" />
                    <span className="line-clamp-2">{plugin.error}</span>
                  </div>
                )}

                {/* 操作按钮区 */}
                <div className="flex items-center gap-2">
                  {/* 配置入口提示（有 config_files 时提示在左侧「插件配置」区编辑） */}
                  {hasConfig && (
                    <span className="text-muted-foreground text-[10px]">
                      {plugin.config_files.length} 个配置文件（在左侧「插件配置」编辑）
                    </span>
                  )}
                  {/* 禁用状态标注 */}
                  {!plugin.enabled && (
                    <span className="text-muted-foreground flex items-center gap-1 text-[10px]">
                      <ToggleLeft className="h-3 w-3" />
                      已禁用（不贡献工具/路由/UI）
                    </span>
                  )}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )

  if (embedded) {
    return (
      <PageShell title="插件管理" embedded>
        {mainContent}
      </PageShell>
    )
  }

  return (
    <PageShell
      title="插件管理"
      backHref="/settings"
      backLabel="返回设置"
      actions={
        <span className="text-muted-foreground font-mono text-xs">
          {plugins.filter((p) => p.enabled).length}/{plugins.length} 启用
        </span>
      }
    >
      {mainContent}
    </PageShell>
  )
}
